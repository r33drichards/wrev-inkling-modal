package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

const reviewTimeout = 10 * time.Minute

var (
	reviewLog  = log.New(os.Stderr, "", log.LstdFlags)
	reviewRepo = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)
	reviewSHA  = regexp.MustCompile(`^[a-fA-F0-9]{40}$`)
	deliveryID = regexp.MustCompile(`^[A-Za-z0-9-]{1,128}$`)
)

type reviewService struct {
	db          *sql.DB
	credentials *reviewCredentials
	comments    *reviewComments
	binary      string
	data        string
	owner       string
	label       string
	signingKey  []byte
	wake        chan struct{}
	mu          sync.Mutex
	cancel      map[int64]context.CancelFunc
}

func openReviewDB(path string, schema string) (*sql.DB, error) {
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000; " + schema); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func openReviewService(binary string) (*reviewService, error) {
	binary, err := exec.LookPath(binary)
	if err != nil {
		return nil, err
	}
	data := os.Getenv("WREV_DATA_DIR")
	owner := os.Getenv("WREV_REPOSITORY_OWNER")
	secret := os.Getenv("WREV_WEBHOOK_SECRET")
	if data == "" || !reviewRepo.MatchString(owner) || len(secret) < 32 {
		return nil, errors.New("set WREV_DATA_DIR, WREV_REPOSITORY_OWNER, and a strong WREV_WEBHOOK_SECRET")
	}
	if err := os.MkdirAll(filepath.Join(data, "sessions-native"), 0700); err != nil {
		return nil, err
	}
	credentials, err := openReviewCredentials(filepath.Join(data, "credentials.sqlite"))
	if err != nil {
		return nil, err
	}
	db, err := openReviewDB(filepath.Join(data, "queue.sqlite"), `
		CREATE TABLE IF NOT EXISTS review_comments (review_id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL);
		CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY);
		CREATE TABLE IF NOT EXISTS reviews (
			id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL,
			state TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, result TEXT
		);
		CREATE UNIQUE INDEX IF NOT EXISTS active_review ON reviews(repository, number)
			WHERE state IN ('queued', 'running');
		UPDATE reviews SET state = 'queued' WHERE state = 'running';`)
	if err != nil {
		credentials.db.Close()
		return nil, err
	}
	label := os.Getenv("WREV_LABEL")
	if label == "" {
		label = "wrev"
	}
	return &reviewService{
		db: db, credentials: credentials, comments: newReviewComments(os.Getenv("WREV_GITHUB_COMMENT_TOKEN")), binary: binary, data: data, owner: strings.ToLower(owner),
		label: label, signingKey: []byte(secret), wake: make(chan struct{}, 1), cancel: make(map[int64]context.CancelFunc),
	}, nil
}

type reviewEvent struct {
	Action string `json:"action"`
	Number int64  `json:"number"`
	Label  struct {
		Name string `json:"name"`
	} `json:"label"`
	Repository struct {
		FullName string `json:"full_name"`
		Owner    struct {
			Login string `json:"login"`
		} `json:"owner"`
	} `json:"repository"`
	PullRequest struct {
		Head struct {
			SHA string `json:"sha"`
		} `json:"head"`
	} `json:"pull_request"`
}

func (s *reviewService) githubWebhook(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		return
	}
	mac := hmac.New(sha256.New, s.signingKey)
	mac.Write(body)
	signature, ok := strings.CutPrefix(r.Header.Get("X-Hub-Signature-256"), "sha256=")
	provided, err := hex.DecodeString(signature)
	if !ok || err != nil || !hmac.Equal(provided, mac.Sum(nil)) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	if r.Header.Get("X-GitHub-Event") != "pull_request" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	var event reviewEvent
	delivery := r.Header.Get("X-GitHub-Delivery")
	if err := json.Unmarshal(body, &event); err != nil || !deliveryID.MatchString(delivery) {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	if event.Label.Name != s.label || (event.Action != "labeled" && event.Action != "unlabeled") {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	repo := strings.ToLower(event.Repository.FullName)
	owner, name, ok := strings.Cut(repo, "/")
	if !ok || owner != s.owner || !strings.EqualFold(event.Repository.Owner.Login, s.owner) ||
		!reviewRepo.MatchString(name) || name == "." || name == ".." {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	if event.Number <= 0 || (event.Action == "labeled" && !reviewSHA.MatchString(event.PullRequest.Head.SHA)) {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	accepted, err := s.apply(delivery, repo, event)
	if err != nil {
		reviewLog.Printf("could not persist review event: %v", err)
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if accepted {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *reviewService) apply(delivery, repo string, event reviewEvent) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	insert, err := tx.Exec("INSERT OR IGNORE INTO deliveries(id) VALUES (?)", delivery)
	if err != nil {
		return false, err
	}
	if count, _ := insert.RowsAffected(); count == 0 {
		return false, nil
	}
	var canceled int64
	if event.Action == "labeled" {
		_, err = tx.Exec(`INSERT OR IGNORE INTO reviews(repository, number, head, state)
			VALUES (?, ?, ?, 'queued')`, repo, event.Number, event.PullRequest.Head.SHA)
	} else {
		err = tx.QueryRow(`UPDATE reviews SET state = 'canceled'
			WHERE repository = ? AND number = ? AND state IN ('queued', 'running') RETURNING id`,
			repo, event.Number).Scan(&canceled)
		if errors.Is(err, sql.ErrNoRows) {
			err = nil
		}
	}
	if err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	if cancel := s.cancel[canceled]; cancel != nil {
		cancel()
	}
	select {
	case s.wake <- struct{}{}:
	default:
	}
	return true, nil
}

type reviewJob struct {
	id         int64
	repository string
	number     int64
	head       string
}

func (s *reviewService) claim() (reviewJob, context.Context, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var job reviewJob
	err := s.db.QueryRow(`UPDATE reviews SET state = 'running'
		WHERE id = (SELECT id FROM reviews WHERE state = 'queued' ORDER BY id LIMIT 1)
		RETURNING id, repository, number, head`).Scan(&job.id, &job.repository, &job.number, &job.head)
	if err != nil {
		return job, nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), reviewTimeout)
	s.cancel[job.id] = cancel
	return job, ctx, nil
}

func (s *reviewService) work() {
	for {
		job, ctx, err := s.claim()
		if errors.Is(err, sql.ErrNoRows) {
			<-s.wake
			continue
		}
		if err != nil {
			reviewLog.Printf("could not claim review: %v", err)
			time.Sleep(time.Second)
			continue
		}
		s.notifyReview(ctx, job, "started", "")
		result, err := s.run(ctx, job)
		state := "completed"
		if err != nil {
			state = "failed"
			reviewLog.Printf("review %d did not finish: %v", job.id, err)
		}
		s.mu.Lock()
		s.cancel[job.id]()
		delete(s.cancel, job.id)
		updated, err := s.db.Exec("UPDATE reviews SET state = ?, result = NULLIF(?, '') WHERE id = ? AND state = 'running'",
			state, result, job.id)
		if err == nil {
			var count int64
			count, err = updated.RowsAffected()
			if err == nil && count == 0 {
				err = s.db.QueryRow("SELECT state FROM reviews WHERE id = ?", job.id).Scan(&state)
			}
		}
		s.mu.Unlock()
		if err != nil {
			reviewLog.Printf("could not persist result for review %d: %v", job.id, err)
			continue
		}
		s.notifyReview(context.Background(), job, state, result)
		if state == "completed" {
			s.removeCompletedLabel(job)
		}
	}
}

func (s *reviewService) removeCompletedLabel(job reviewJob) {
	if s.comments == nil {
		return
	}
	var newest int64
	err := s.db.QueryRow("SELECT MAX(id) FROM reviews WHERE repository = ? AND number = ?", job.repository, job.number).Scan(&newest)
	if err == nil && newest == job.id {
		err = s.comments.removeLabel(context.Background(), job, s.label)
	}
	if err != nil {
		reviewLog.Printf("could not remove trigger label for review %d: %v", job.id, err)
	}
}

func (s *reviewService) notifyReview(ctx context.Context, job reviewJob, state, result string) {
	if s.comments == nil {
		return
	}
	var commentID int64
	err := s.db.QueryRow("SELECT comment_id FROM review_comments WHERE review_id = ?", job.id).Scan(&commentID)
	if err == nil && state == "started" {
		return
	}
	if err == nil || errors.Is(err, sql.ErrNoRows) {
		var id int64
		id, err = s.comments.send(ctx, job, commentID, reviewCommentBody(job, state, result))
		if err == nil && commentID == 0 {
			_, err = s.db.Exec("INSERT INTO review_comments (review_id, comment_id) VALUES (?, ?)", job.id, id)
		}
	}
	if err != nil {
		reviewLog.Printf("could not update PR comment for review %d: %v", job.id, err)
	}
}

func (s *reviewService) run(ctx context.Context, job reviewJob) (string, error) {
	if os.Getenv("WREV_GITHUB_TOKEN") == "" || os.Getenv("OPENAI_MODEL") == "" {
		return "", errors.New("configure WREV_GITHUB_TOKEN and OPENAI_MODEL")
	}
	token, err := s.credentials.accessToken(ctx)
	if err != nil {
		return "", err
	}
	dir := filepath.Join(s.data, "sessions-native", strconv.FormatInt(job.id, 10))
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	scratch, err := os.MkdirTemp("", "wrev-checkout-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(scratch)
	env := []string{"PATH=" + os.Getenv("PATH"), "TMPDIR=" + scratch, "WREV_SESSION_DIR=" + dir, "WREV_EXPECTED_HEAD=" + job.head,
		"WREV_REQUIRED_LABEL=" + s.label, "CHATGPT_ACCESS_TOKEN=" + token}
	for _, key := range []string{"XDG_CACHE_HOME", "WREV_GITHUB_TOKEN", "OPENAI_MODEL",
		"WREV_MODEL_TIMEOUT_SECS", "WREV_GITHUB_TIMEOUT_SECS", "WREV_SCRIPT_TIMEOUT_SECS",
		"WREV_SCRIPT_CODE_BYTES", "WREV_SCRIPT_OUTPUT_BYTES", "WREV_SCRIPT_MEMORY_BYTES", "WREV_SCRIPT_STACK_BYTES"} {
		if value, ok := os.LookupEnv(key); ok {
			env = append(env, key+"="+value)
		}
	}
	cmd := exec.CommandContext(ctx, s.binary, "review", job.repository, strconv.FormatInt(job.number, 10))
	cmd.Dir = dir
	cmd.Env = env
	configureReviewCancellation(cmd)
	output, err := cmd.Output()
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			message := strings.TrimSpace(string(exit.Stderr))
			if len(message) > 2048 {
				message = message[:2048]
			}
			return "", fmt.Errorf("wrev process: %w: %s", err, message)
		}
		return "", fmt.Errorf("wrev process: %w", err)
	}
	if !json.Valid(output) {
		return "", errors.New("wrev returned invalid review JSON")
	}
	return string(output), nil
}

func (s *reviewService) list(w http.ResponseWriter, r *http.Request) {
	if !s.credentials.authorized(r.Header.Get("Authorization")) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	rows, err := s.db.Query(`SELECT id, repository, number, head, state, created_at, COALESCE(NULLIF(result, ''), 'null')
		FROM reviews ORDER BY id DESC LIMIT 100`)
	if err != nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	defer rows.Close()
	results := []map[string]interface{}{}
	for rows.Next() {
		var job reviewJob
		var state, created, result string
		if err := rows.Scan(&job.id, &job.repository, &job.number, &job.head, &state, &created, &result); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		results = append(results, map[string]interface{}{"id": job.id, "repository": job.repository, "number": job.number,
			"head": job.head, "state": state, "created_at": created, "result": json.RawMessage(result)})
	}
	if rows.Err() != nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(results)
}
