package main

import (
	"encoding/json"
	"fmt"
)

// Command is a Python→bridge frame. One struct for all inbound types keeps
// decoding trivial; Type discriminates. Field names must match the legacy
// Node-bridge protocol (camelCase media fields).
type Command struct {
	Type     string `json:"type"`
	Token    string `json:"token,omitempty"`
	ID       string `json:"id,omitempty"`
	To       string `json:"to,omitempty"`
	Text     string `json:"text,omitempty"`
	ReplyTo  string `json:"reply_to,omitempty"`
	FilePath string `json:"filePath,omitempty"`
	Mimetype string `json:"mimetype,omitempty"`
	FileName string `json:"fileName,omitempty"`
	State    string `json:"state,omitempty"`
}

func DecodeCommand(raw []byte) (Command, error) {
	var c Command
	if err := json.Unmarshal(raw, &c); err != nil {
		return Command{}, fmt.Errorf("invalid frame: %w", err)
	}
	if c.Type == "" {
		return Command{}, fmt.Errorf("frame missing type")
	}
	return c, nil
}

// Bridge→Python frames.

type Ack struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

func NewAck(id string, err error) Ack {
	if err != nil {
		return Ack{Type: "ack", ID: id, OK: false, Error: err.Error()}
	}
	return Ack{Type: "ack", ID: id, OK: true}
}

type Quoted struct {
	ID     string `json:"id"`
	Sender string `json:"sender"`
	Text   string `json:"text"`
}

type Message struct {
	Type         string   `json:"type"`
	PN           string   `json:"pn"`
	Sender       string   `json:"sender"`
	Content      string   `json:"content"`
	ID           string   `json:"id"`
	IsGroup      bool     `json:"isGroup"`
	WasMentioned bool     `json:"wasMentioned"`
	Media        []string `json:"media"`
	Timestamp    int64    `json:"timestamp"`
	Voice        bool     `json:"voice"`
	Quoted       *Quoted  `json:"quoted,omitempty"`
}

type Status struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Version string `json:"version,omitempty"`
}

type QR struct {
	Type string `json:"type"`
	Code string `json:"code"`
}

type ErrorFrame struct {
	Type  string `json:"type"`
	Error string `json:"error"`
}
