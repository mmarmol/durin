package main

import (
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/types"
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

func TestMentionMatchesOwnUserMatchesPhone(t *testing.T) {
	if !mentionMatchesOwnUser("123", "123", "456") {
		t.Fatal("expected phone-user match")
	}
}

func TestMentionMatchesOwnUserMatchesLID(t *testing.T) {
	if !mentionMatchesOwnUser("456", "123", "456") {
		t.Fatal("expected LID-user match")
	}
}

func TestMentionMatchesOwnUserNoMatch(t *testing.T) {
	if mentionMatchesOwnUser("999", "123", "456") {
		t.Fatal("expected no match for unrelated user")
	}
}

func TestMentionMatchesOwnUserEmptyLIDGuarded(t *testing.T) {
	// An empty own-LID must never match an empty mentioned user.
	if mentionMatchesOwnUser("", "123", "") {
		t.Fatal("empty mentioned user must never match")
	}
}

func TestBuildTextMessagePlainWhenNoParticipant(t *testing.T) {
	msg := buildTextMessage("hello", "STANZA1", "")
	if msg.GetConversation() != "hello" {
		t.Fatalf("expected plain conversation message, got %+v", msg)
	}
	if msg.GetExtendedTextMessage() != nil {
		t.Fatalf("must not emit a quote without a known participant: %+v", msg)
	}
}

func TestBuildTextMessageQuotedWithParticipant(t *testing.T) {
	msg := buildTextMessage("hello", "STANZA1", "555@s.whatsapp.net")
	ext := msg.GetExtendedTextMessage()
	if ext == nil {
		t.Fatalf("expected an extended text message with a quote, got %+v", msg)
	}
	ci := ext.GetContextInfo()
	if ci.GetStanzaID() != "STANZA1" || ci.GetParticipant() != "555@s.whatsapp.net" {
		t.Fatalf("unexpected context info: %+v", ci)
	}
}

func TestResolveReplyParticipantDMDefaultsToDestination(t *testing.T) {
	jid := types.JID{User: "555", Server: types.DefaultUserServer}
	got := resolveReplyParticipant("", jid)
	if got != jid.String() {
		t.Fatalf("expected DM participant to default to the destination JID, got %q", got)
	}
}

func TestResolveReplyParticipantGroupWithKnownParticipant(t *testing.T) {
	jid := types.JID{User: "12345", Server: types.GroupServer}
	got := resolveReplyParticipant("555@s.whatsapp.net", jid)
	if got != "555@s.whatsapp.net" {
		t.Fatalf("expected the known participant to be used verbatim, got %q", got)
	}
}

func TestResolveReplyParticipantGroupWithoutKnownParticipantStaysEmpty(t *testing.T) {
	jid := types.JID{User: "12345", Server: types.GroupServer}
	got := resolveReplyParticipant("", jid)
	if got != "" {
		t.Fatalf("group JID must never be used as the fallback participant, got %q", got)
	}
}
