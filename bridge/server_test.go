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

func TestNewClientReplacesOld(t *testing.T) {
	s := NewServer("secret", "test")
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	ws1 := dial(t, ts.URL)
	defer ws1.Close()
	ws1.WriteMessage(websocket.TextMessage, []byte(`{"type":"auth","token":"secret"}`))
	ws1.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, _, err := ws1.ReadMessage(); err != nil { // welcome status
		t.Fatal(err)
	}

	ws2 := dial(t, ts.URL)
	defer ws2.Close()
	ws2.WriteMessage(websocket.TextMessage, []byte(`{"type":"auth","token":"secret"}`))
	ws2.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, _, err := ws2.ReadMessage(); err != nil { // welcome status
		t.Fatal(err)
	}

	// The replaced first client must see its connection closed.
	ws1.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, _, err := ws1.ReadMessage(); err == nil {
		t.Fatal("expected first client to be closed after replacement")
	}

	// Send must reach the second (current) client.
	if err := s.Send(NewAck("2", nil)); err != nil {
		t.Fatal(err)
	}
	_, raw, err := ws2.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"type":"ack"`) || !strings.Contains(string(raw), `"id":"2"`) {
		t.Fatalf("expected ack for id 2, got %s", raw)
	}
}
