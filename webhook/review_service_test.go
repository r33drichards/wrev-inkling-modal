//go:build cgo

package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func reviewTestService(t *testing.T) *reviewService {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("WREV_DATA_DIR", dir)
	t.Setenv("WREV_REPOSITORY_OWNER", "example")
	t.Setenv("WREV_WEBHOOK_SECRET", strings.Repeat("w", 32))
	t.Setenv("WREV_CREDENTIAL_ADMIN_TOKEN", strings.Repeat("a", 32))
	t.Setenv("WREV_CREDENTIAL_ENCRYPTION_KEY", strings.Repeat("b", 64))
	binary := filepath.Join(dir, "wrev")
	if err := os.WriteFile(binary, nil, 0700); err != nil {
		t.Fatal(err)
	}
	service, err := openReviewService(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { service.db.Close(); service.credentials.db.Close() })
	return service
}

func TestLabelSessionLifecycle(t *testing.T) {
	service := reviewTestService(t)
	send := func(action, delivery, owner string, signed bool) int {
		body := fmt.Sprintf(`{"action":%q,"number":14,"label":{"name":"wrev"},"repository":{"full_name":%q,"owner":{"login":%q}},"pull_request":{"head":{"sha":%q}}}`,
			action, owner+"/project", owner, strings.Repeat("1", 40))
		req := httptest.NewRequest(http.MethodPost, "/github/webhook", strings.NewReader(body))
		req.Header.Set("X-GitHub-Event", "pull_request")
		req.Header.Set("X-GitHub-Delivery", delivery)
		if signed {
			mac := hmac.New(sha256.New, service.signingKey)
			mac.Write([]byte(body))
			req.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
		}
		response := httptest.NewRecorder()
		service.githubWebhook(response, req)
		return response.Code
	}
	if send("labeled", "unsigned", "example", false) != 401 || send("labeled", "outside", "other", true) != 403 {
		t.Fatal("accepted an unauthorized event")
	}
	if send("labeled", "start", "example", true) != 202 || send("labeled", "start", "example", true) != 204 {
		t.Fatal("failed to accept or deduplicate a label event")
	}
	job, ctx, err := service.claim()
	if err != nil {
		t.Fatal(err)
	}
	if send("unlabeled", "stop", "example", true) != 202 || ctx.Err() != context.Canceled {
		t.Fatal("unlabel did not cancel the active worker")
	}
	var state string
	if err := service.db.QueryRow("SELECT state FROM reviews WHERE id = ?", job.id).Scan(&state); err != nil || state != "canceled" {
		t.Fatalf("expected persisted cancellation, got %q (%v)", state, err)
	}
	if send("labeled", "restart", "example", true) != 202 {
		t.Fatal("relabel did not start a new session")
	}
	newJob, _, err := service.claim()
	if err != nil || newJob.id == job.id {
		t.Fatalf("expected new session, got %+v (%v)", newJob, err)
	}
	service.cancel[newJob.id]()
	if _, _, err := service.claim(); err != sql.ErrNoRows {
		t.Fatalf("unexpected extra job: %v", err)
	}
}

func TestSQLiteReviewCredentials(t *testing.T) {
	service := reviewTestService(t)
	body := fmt.Sprintf(`{"access_token":"fixture-access","expires_at":%d}`, time.Now().Add(time.Hour).Unix())
	req := httptest.NewRequest(http.MethodPut, "/admin/chatgpt-credentials", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+strings.Repeat("a", 32))
	response := httptest.NewRecorder()
	service.credentials.put(response, req)
	if response.Code != 204 {
		t.Fatalf("put returned %d", response.Code)
	}
	var raw string
	if err := service.credentials.db.QueryRow("SELECT sealed FROM credentials").Scan(&raw); err != nil || strings.Contains(raw, "fixture-access") {
		t.Fatalf("credentials were not sealed: %v", err)
	}
	token, err := service.credentials.accessToken(context.Background())
	if err != nil || token != "fixture-access" {
		t.Fatalf("could not restore encrypted credentials: %v", err)
	}
}

func TestReviewListIncludesFailedRowsWithoutResults(t *testing.T) {
	service := reviewTestService(t)
	_, err := service.db.Exec("INSERT INTO reviews (repository, number, head, state, result) VALUES ('example/project', 1, 'test', 'failed', '')")
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/admin/reviews", nil)
	req.Header.Set("Authorization", "Bearer "+strings.Repeat("a", 32))
	response := httptest.NewRecorder()
	service.list(response, req)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"result":null`) {
		t.Fatalf("expected failed review with a null result, got %d %q", response.Code, response.Body.String())
	}
}

func TestReviewCommentsReuseSessionComment(t *testing.T) {
	service := reviewTestService(t)
	var requests []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-comment-token" {
			t.Error("missing comment credential")
		}
		requests = append(requests, r.Method+" "+r.URL.Path)
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusCreated)
		}
		fmt.Fprint(w, `{"id":42}`)
	}))
	defer server.Close()
	service.comments = newReviewComments("test-comment-token")
	service.comments.api = server.URL
	job := reviewJob{id: 1, repository: "example/project", number: 14, head: strings.Repeat("1", 40)}
	service.notifyReview(context.Background(), job, "started", "")
	service.notifyReview(context.Background(), job, "started", "")
	result := `{"findings":[{"file":"src/main.rs","start_line":3,"end_line":4,"message":"First line\n@someone <img>"}]}`
	service.notifyReview(context.Background(), job, "completed", result)
	want := "POST /repos/example/project/issues/14/comments,PATCH /repos/example/project/issues/comments/42"
	if strings.Join(requests, ",") != want {
		t.Fatalf("unexpected GitHub calls: %v", requests)
	}
	body := reviewCommentBody(job, "completed", result)
	if !strings.Contains(body, "    1. src/main.rs:3–4") || !strings.Contains(body, "\n    @someone <img>") {
		t.Fatalf("expected findings rendered as plain code: %q", body)
	}
}

func TestCompletedReviewRemovesOnlyItsTriggerLabel(t *testing.T) {
	service := reviewTestService(t)
	service.label = "review/auto"
	requests := make(chan string, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- r.Method + " " + r.URL.EscapedPath()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	service.comments = newReviewComments("test-comment-token")
	service.comments.api = server.URL
	if _, err := service.db.Exec(`INSERT INTO reviews (id, repository, number, head, state)
		VALUES (1, 'example/project', 14, 'test', 'completed'), (2, 'example/project', 14, 'test', 'completed')`); err != nil {
		t.Fatal(err)
	}
	job := reviewJob{id: 1, repository: "example/project", number: 14}
	service.removeCompletedLabel(job)
	job.id = 2
	service.removeCompletedLabel(job)
	if len(requests) != 1 {
		t.Fatalf("expected exactly one label removal, got %d", len(requests))
	}
	if request := <-requests; request != "DELETE /repos/example/project/issues/14/labels/review%2Fauto" {
		t.Fatalf("unexpected label removal request: %s", request)
	}
}
