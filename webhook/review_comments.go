package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type reviewComments struct {
	token  string
	api    string
	client *http.Client
}

func newReviewComments(token string) *reviewComments {
	if token == "" {
		return nil
	}
	return &reviewComments{token: token, api: "https://api.github.com", client: &http.Client{
		Timeout:       8 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (c *reviewComments) send(ctx context.Context, job reviewJob, commentID int64, body string) (int64, error) {
	url := fmt.Sprintf("%s/repos/%s/issues/%d/comments", c.api, job.repository, job.number)
	method := http.MethodPost
	if commentID != 0 {
		url = fmt.Sprintf("%s/repos/%s/issues/comments/%d", c.api, job.repository, commentID)
		method = http.MethodPatch
	}
	payload, err := json.Marshal(map[string]string{"body": body})
	if err != nil {
		return 0, err
	}
	request, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("Content-Type", "application/json")
	response, err := c.client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("GitHub comment API returned HTTP %d", response.StatusCode)
	}
	var comment struct{ ID int64 }
	err = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&comment)
	if err != nil {
		return 0, err
	}
	if comment.ID <= 0 {
		return 0, fmt.Errorf("GitHub returned no comment ID")
	}
	return comment.ID, nil
}

func (c *reviewComments) removeLabel(ctx context.Context, job reviewJob, label string) error {
	endpoint := fmt.Sprintf("%s/repos/%s/issues/%d/labels/%s", c.api, job.repository, job.number, url.PathEscape(label))
	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, endpoint, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Accept", "application/vnd.github+json")
	response, err := c.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusOK, http.StatusNoContent:
		return nil
	case http.StatusNotFound:
		// GitHub also returns 404 when the label has already been removed.
		return nil
	default:
		return fmt.Errorf("GitHub label API returned HTTP %d", response.StatusCode)
	}
}

func reviewCommentBody(job reviewJob, state, result string) string {
	header := fmt.Sprintf("**Wrev review %s** for `%s` · session %d", state, job.head, job.id)
	if state != "completed" {
		return header
	}
	var review struct {
		Findings []struct {
			File    string
			Start   int64 `json:"start_line"`
			End     int64 `json:"end_line"`
			Message string
		}
	}
	if json.Unmarshal([]byte(result), &review) != nil || review.Findings == nil {
		return header + "\n\nSee the stored review result for details."
	}
	if len(review.Findings) == 0 {
		return header + "\n\nNo findings."
	}
	var body strings.Builder
	fmt.Fprintf(&body, "%s\n\n%d finding(s)\n", header, len(review.Findings))
	for i, finding := range review.Findings {
		// Render model-generated text as code so it cannot inject mentions or markup.
		text := strings.ReplaceAll(fmt.Sprintf("%d. %s:%d–%d\n%s", i+1, finding.File, finding.Start, finding.End, finding.Message), "\r", "")
		entry := "\n    " + strings.ReplaceAll(text, "\n", "\n    ") + "\n"
		if body.Len()+len(entry) > 60_000 {
			body.WriteString("\nAdditional findings are available in the stored review result.")
			break
		}
		body.WriteString(entry)
	}
	return body.String()
}
