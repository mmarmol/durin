package main

import (
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func dial(t *testing.T, url string) *websocket.Conn {
	t.Helper()
	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(url, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func TestRejectsBadToken(t *testing.T) {
	s := NewServer("secret", "test")
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	ws := dial(t, ts.URL)
	defer ws.Close()
	ws.WriteMessage(websocket.TextMessage, []byte(`{"type":"auth","token":"wrong"}`))
	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, _, err := ws.ReadMessage(); err == nil {
		t.Fatal("expected close on bad token")
	}
}

func TestAuthThenStatusAndCommands(t *testing.T) {
	s := NewServer("secret", "test")
	s.SetConnected(true)
	got := make(chan Command, 1)
	s.OnCommand = func(c Command) { got <- c }
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	ws := dial(t, ts.URL)
	defer ws.Close()
	ws.WriteMessage(websocket.TextMessage, []byte(`{"type":"auth","token":"secret"}`))
	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, raw, err := ws.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"status":"connected"`) || !strings.Contains(string(raw), `"version":"test"`) {
		t.Fatalf("expected connected status with version, got %s", raw)
	}
	ws.WriteMessage(websocket.TextMessage, []byte(`{"type":"send","id":"1","to":"x","text":"hi"}`))
	select {
	case c := <-got:
		if c.Type != "send" || c.ID != "1" {
			t.Fatalf("bad command: %+v", c)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("command not delivered")
	}
	// Server → client push.
	if err := s.Send(NewAck("1", nil)); err != nil {
		t.Fatal(err)
	}
	_, raw, err = ws.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"type":"ack"`) {
		t.Fatalf("expected ack, got %s", raw)
	}
}
