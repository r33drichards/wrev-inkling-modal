package main

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

type reviewCredentials struct {
	db    *sql.DB
	aead  cipher.AEAD
	admin [sha256.Size]byte
	mu    sync.Mutex
}

type chatGPTGrant struct {
	AccessToken  string `json:"access_token"`
	ExpiresAt    int64  `json:"expires_at"`
	RefreshToken string `json:"refresh_token,omitempty"`
	ClientID     string `json:"client_id,omitempty"`
}

var credentialAAD = []byte("wrev/chatgpt-credentials/v1")

func openReviewCredentials(path string) (*reviewCredentials, error) {
	admin := os.Getenv("WREV_CREDENTIAL_ADMIN_TOKEN")
	key, err := hex.DecodeString(os.Getenv("WREV_CREDENTIAL_ENCRYPTION_KEY"))
	if err != nil || len(key) != 32 || len(admin) < 32 || !graphic(admin) {
		return nil, errors.New("set WREV_CREDENTIAL_ADMIN_TOKEN and a 32-byte hex WREV_CREDENTIAL_ENCRYPTION_KEY")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	db, err := openReviewDB(path, `CREATE TABLE IF NOT EXISTS credentials (
		id INTEGER PRIMARY KEY CHECK (id = 1), sealed BLOB NOT NULL);`)
	if err != nil {
		return nil, err
	}
	return &reviewCredentials{db: db, aead: aead, admin: sha256.Sum256([]byte(admin))}, nil
}

func graphic(value string) bool {
	for _, char := range []byte(value) {
		if char < 33 || char > 126 {
			return false
		}
	}
	return value != ""
}

func (g chatGPTGrant) valid() bool {
	return graphic(g.AccessToken) && g.ExpiresAt > 0 &&
		((g.RefreshToken == "" && g.ClientID == "") || (graphic(g.RefreshToken) && graphic(g.ClientID)))
}

func (c *reviewCredentials) authorized(header string) bool {
	token, ok := strings.CutPrefix(header, "Bearer ")
	digest := sha256.Sum256([]byte(token))
	return ok && subtle.ConstantTimeCompare(digest[:], c.admin[:]) == 1
}

func (c *reviewCredentials) put(w http.ResponseWriter, r *http.Request) {
	if !c.authorized(r.Header.Get("Authorization")) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 32<<10))
	if err != nil {
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var grant chatGPTGrant
	if decoder.Decode(&grant) != nil || decoder.Decode(new(interface{})) != io.EOF || !grant.valid() ||
		(grant.ExpiresAt <= time.Now().Unix() && grant.RefreshToken == "") {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	err = c.save(r.Context(), grant)
	c.mu.Unlock()
	if err != nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (c *reviewCredentials) save(ctx context.Context, grant chatGPTGrant) error {
	payload, err := json.Marshal(grant)
	if err != nil {
		return err
	}
	nonce := make([]byte, c.aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	sealed := c.aead.Seal(nonce, nonce, payload, credentialAAD)
	_, err = c.db.ExecContext(ctx, `INSERT INTO credentials(id, sealed) VALUES (1, ?)
		ON CONFLICT(id) DO UPDATE SET sealed = excluded.sealed`, sealed)
	return err
}

func (c *reviewCredentials) accessToken(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var sealed []byte
	err := c.db.QueryRowContext(ctx, "SELECT sealed FROM credentials WHERE id = 1").Scan(&sealed)
	if errors.Is(err, sql.ErrNoRows) {
		if token := os.Getenv("CHATGPT_ACCESS_TOKEN"); graphic(token) {
			return token, nil
		}
		return "", errors.New("provision hosted ChatGPT credentials")
	}
	if err != nil {
		return "", err
	}
	if len(sealed) < c.aead.NonceSize() {
		return "", errors.New("invalid credential record")
	}
	nonce := sealed[:c.aead.NonceSize()]
	payload, err := c.aead.Open(nil, nonce, sealed[len(nonce):], credentialAAD)
	if err != nil {
		return "", errors.New("could not decrypt hosted credentials")
	}
	var grant chatGPTGrant
	if json.Unmarshal(payload, &grant) != nil || !grant.valid() {
		return "", errors.New("invalid hosted credential record")
	}
	now := time.Now()
	if grant.ExpiresAt > now.Add(reviewTimeout).Unix() ||
		(grant.RefreshToken == "" && grant.ExpiresAt > now.Unix()) {
		return grant.AccessToken, nil
	}
	if grant.RefreshToken == "" {
		return "", errors.New("hosted ChatGPT credential expired")
	}
	// Persist retirement before exchanging the rotating refresh token. An
	// interrupted exchange leaves the old refresh token retired.
	retired := grant
	retired.RefreshToken, retired.ClientID = "", ""
	if err := c.save(ctx, retired); err != nil {
		return "", err
	}
	refreshCtx, stopRefresh := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer stopRefresh()
	next, err := grant.refresh(refreshCtx)
	if err != nil {
		return "", err
	}
	// Preserve the replacement even if a concurrent label removal canceled
	// the worker after the exchange completed.
	saveCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := c.save(saveCtx, next); err != nil {
		return "", err
	}
	return next.AccessToken, nil
}

func (g chatGPTGrant) refresh(ctx context.Context) (chatGPTGrant, error) {
	form := url.Values{"grant_type": {"refresh_token"}, "client_id": {g.ClientID},
		"refresh_token": {g.RefreshToken}, "resource": {"https://api.openai.com/v1"}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		"https://auth.openai.com/api/accounts/oauth/token", strings.NewReader(form.Encode()))
	if err != nil {
		return chatGPTGrant{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	client := http.Client{Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	resp, err := client.Do(req)
	if err != nil {
		return chatGPTGrant{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return chatGPTGrant{}, fmt.Errorf("ChatGPT credential refresh returned HTTP %d", resp.StatusCode)
	}
	var data struct {
		AccessToken  string  `json:"access_token"`
		RefreshToken string  `json:"refresh_token"`
		TokenType    string  `json:"token_type"`
		ExpiresIn    int64   `json:"expires_in"`
		Scope        *string `json:"scope"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 32<<10)).Decode(&data) != nil ||
		!strings.EqualFold(data.TokenType, "bearer") || data.ExpiresIn <= 30 || data.ExpiresIn > 365*24*60*60 {
		return chatGPTGrant{}, errors.New("invalid ChatGPT credential refresh response")
	}
	if data.Scope != nil {
		hasPermission := false
		for _, scope := range strings.Fields(*data.Scope) {
			hasPermission = hasPermission || scope == "chatgpt.tokens.use.direct"
		}
		if !hasPermission {
			return chatGPTGrant{}, errors.New("refreshed grant lacks ChatGPT plan permission")
		}
	}
	next := chatGPTGrant{data.AccessToken, time.Now().Unix() + data.ExpiresIn, data.RefreshToken, g.ClientID}
	if !next.valid() {
		return chatGPTGrant{}, errors.New("invalid replacement ChatGPT credential")
	}
	return next, nil
}
