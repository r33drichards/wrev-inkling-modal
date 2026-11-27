//go:build !windows

package main

import (
	"context"
	"os/exec"
	"testing"
	"time"
)

func TestNativeReviewCancellationClosesDescendantPipes(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", "sleep 30 & wait")
	configureReviewCancellation(cmd)
	start := time.Now()
	if _, err := cmd.Output(); err == nil || ctx.Err() != context.DeadlineExceeded {
		t.Fatalf("expected subprocess cancellation, got %v", err)
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("subprocess cancellation took %s", elapsed)
	}
}
