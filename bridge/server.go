package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Server is a single-client loopback WS relay. The first frame must be an
// auth command carrying the shared token; everything after is relayed via
// OnCommand. Send pushes frames to the authed client.
type Server struct {
	token     string
	version   string
	OnCommand func(Command)

	mu        sync.Mutex
	conn      *websocket.Conn
	connected bool
}

func NewServer(token, version string) *Server {
	return &Server{token: token, version: version}
}

var upgrader = websocket.Upgrader{
	// Loopback only; no cross-origin browsers involved.
	CheckOrigin: func(r *http.Request) bool { return true },
}

func (s *Server) SetConnected(v bool) {
	s.mu.Lock()
	s.connected = v
	s.mu.Unlock()
}

func (s *Server) statusFrame() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := "disconnected"
	if s.connected {
		st = "connected"
	}
	return Status{Type: "status", Status: st, Version: s.version}
}

func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		ws.SetReadDeadline(time.Now().Add(10 * time.Second))
		_, raw, err := ws.ReadMessage()
		if err != nil {
			ws.Close()
			return
		}
		cmd, err := DecodeCommand(raw)
		if err != nil || cmd.Type != "auth" ||
			subtle.ConstantTimeCompare([]byte(cmd.Token), []byte(s.token)) != 1 {
			ws.Close()
			return
		}
		ws.SetReadDeadline(time.Time{})

		s.mu.Lock()
		if s.conn != nil {
			s.conn.Close() // newest client wins (gateway reconnect)
		}
		s.conn = ws
		s.mu.Unlock()

		_ = s.Send(s.statusFrame())

		for {
			_, raw, err := ws.ReadMessage()
			if err != nil {
				s.mu.Lock()
				if s.conn == ws {
					s.conn = nil
				}
				s.mu.Unlock()
				return
			}
			cmd, err := DecodeCommand(raw)
			if err != nil {
				_ = s.Send(ErrorFrame{Type: "error", Error: err.Error()})
				continue
			}
			if s.OnCommand != nil {
				s.OnCommand(cmd)
			}
		}
	})
}

// Send marshals v and writes it to the current client, if any.
func (s *Server) Send(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == nil {
		return errors.New("no client connected")
	}
	return s.conn.WriteMessage(websocket.TextMessage, b)
}
