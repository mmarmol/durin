package main

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestDecodeCommandSend(t *testing.T) {
	raw := []byte(`{"type":"send","id":"abc","to":"123@s.whatsapp.net","text":"hola","reply_to":"XYZ"}`)
	cmd, err := DecodeCommand(raw)
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Type != "send" || cmd.ID != "abc" || cmd.To != "123@s.whatsapp.net" || cmd.Text != "hola" || cmd.ReplyTo != "XYZ" {
		t.Fatalf("bad decode: %+v", cmd)
	}
}

func TestDecodeCommandLegacyMediaFields(t *testing.T) {
	raw := []byte(`{"type":"send_media","id":"m1","to":"1@s.whatsapp.net","filePath":"/tmp/a.png","mimetype":"image/png","fileName":"a.png"}`)
	cmd, err := DecodeCommand(raw)
	if err != nil {
		t.Fatal(err)
	}
	if cmd.FilePath != "/tmp/a.png" || cmd.Mimetype != "image/png" || cmd.FileName != "a.png" {
		t.Fatalf("legacy camelCase fields must decode: %+v", cmd)
	}
}

func TestDecodeCommandRejectsMissingType(t *testing.T) {
	if _, err := DecodeCommand([]byte(`{"id":"x"}`)); err == nil {
		t.Fatal("expected error for missing type")
	}
}

func TestAckShape(t *testing.T) {
	ok := NewAck("a1", nil)
	b, _ := json.Marshal(ok)
	if string(b) != `{"type":"ack","id":"a1","ok":true}` {
		t.Fatalf("unexpected ack json: %s", b)
	}
	bad := NewAck("a2", errors.New("boom"))
	b, _ = json.Marshal(bad)
	if !strings.Contains(string(b), `"ok":false`) || !strings.Contains(string(b), `"error":"boom"`) {
		t.Fatalf("unexpected error-ack json: %s", b)
	}
}

func TestMessageFrameLegacyKeys(t *testing.T) {
	m := Message{Type: "message", PN: "1@s.whatsapp.net", Sender: "2@lid.whatsapp.net",
		Content: "hi", ID: "MID", IsGroup: true, WasMentioned: true,
		Media: []string{"/tmp/x.jpg"}, Timestamp: 1720000000, Voice: true,
		Quoted: &Quoted{ID: "Q1", Sender: "3@s.whatsapp.net", Text: "orig"}}
	b, _ := json.Marshal(m)
	for _, key := range []string{`"pn"`, `"sender"`, `"isGroup"`, `"wasMentioned"`, `"media"`, `"voice"`, `"quoted"`} {
		if !strings.Contains(string(b), key) {
			t.Fatalf("missing key %s in %s", key, b)
		}
	}
}

func TestQRAndStatusFrameShapes(t *testing.T) {
	// The gateway parses these NDJSON frames from `qr --emit-frames` stdout to
	// drive the webui pairing UI, so their JSON shape is a wire contract.
	b, _ := json.Marshal(QR{Type: "qr", Code: "wa-code"})
	if string(b) != `{"type":"qr","code":"wa-code"}` {
		t.Fatalf("unexpected qr frame: %s", b)
	}
	b, _ = json.Marshal(Status{Type: "status", Status: "connected"})
	if string(b) != `{"type":"status","status":"connected"}` {
		t.Fatalf("unexpected status frame: %s", b)
	}
}

func TestErrorFrameShape(t *testing.T) {
	b, _ := json.Marshal(ErrorFrame{Type: "error", Error: "boom"})
	if string(b) != `{"type":"error","error":"boom"}` {
		t.Fatalf("unexpected error frame: %s", b)
	}
}
