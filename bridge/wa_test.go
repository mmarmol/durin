package main

import (
	"strings"
	"testing"
)

func TestSanitizeMediaNameStripsPathTraversal(t *testing.T) {
	got := sanitizeMediaName("../../etc/passwd", ".jpg")
	if strings.ContainsAny(got, "/\\") {
		t.Fatalf("sanitized name still contains a path separator: %q", got)
	}
	if !strings.HasSuffix(got, ".jpg") {
		t.Fatalf("sanitized name lost its extension: %q", got)
	}
}

func TestSanitizeMediaNameOnlyAllowsSafeCharset(t *testing.T) {
	got := sanitizeMediaName("weird id!@#$%^&*()=+ name", ".bin")
	for _, r := range strings.TrimSuffix(got, ".bin") {
		safe := (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') ||
			r == '.' || r == '_' || r == '-'
		if !safe {
			t.Fatalf("unsafe character %q leaked into sanitized name %q", r, got)
		}
	}
}

func TestSanitizeMediaNameEmptyIDGetsRandomName(t *testing.T) {
	got1 := sanitizeMediaName("", ".jpg")
	got2 := sanitizeMediaName("", ".jpg")
	if got1 == ".jpg" || got2 == ".jpg" {
		t.Fatalf("empty id must not produce an empty base name: %q / %q", got1, got2)
	}
	if got1 == got2 {
		t.Fatalf("two empty-id sanitizations produced the same random name: %q", got1)
	}
	if !strings.HasSuffix(got1, ".jpg") || !strings.HasSuffix(got2, ".jpg") {
		t.Fatalf("random name lost its extension: %q / %q", got1, got2)
	}
}

func TestSanitizeMediaNameAllDisallowedCharsGetsRandomName(t *testing.T) {
	// Slashes are substituted with underscores rather than dropped, so a
	// slash-only id ends up as underscores, not empty.
	got := sanitizeMediaName("///", ".png")
	if got != "___.png" {
		t.Fatalf("expected slashes substituted with underscores, got %q", got)
	}
}
