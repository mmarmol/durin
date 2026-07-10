package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	_ "modernc.org/sqlite" // pure-Go driver: keeps CGO_ENABLED=0 cross-compiles working
)

func NewWAClient(authDir string) (*whatsmeow.Client, error) {
	if err := os.MkdirAll(authDir, 0o700); err != nil {
		return nil, err
	}
	dbPath := filepath.Join(authDir, "whatsmeow.db")
	container, err := sqlstore.New(context.Background(), "sqlite",
		"file:"+dbPath+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(10000)",
		waLog.Noop)
	if err != nil {
		return nil, fmt.Errorf("open session store: %w", err)
	}
	device, err := container.GetFirstDevice(context.Background())
	if err != nil {
		return nil, fmt.Errorf("load device: %w", err)
	}
	return whatsmeow.NewClient(device, waLog.Stdout("wa", "INFO", true)), nil
}

type Bridge struct {
	cli      *whatsmeow.Client
	srv      *Server
	mediaDir string
}

func NewBridge(cli *whatsmeow.Client, srv *Server, mediaDir string) *Bridge {
	return &Bridge{cli: cli, srv: srv, mediaDir: mediaDir}
}

func (b *Bridge) HandleCommand(cmd Command) {
	switch cmd.Type {
	case "send":
		b.srv.Send(NewAck(cmd.ID, b.sendText(cmd)))
	case "send_media":
		b.srv.Send(NewAck(cmd.ID, b.sendMedia(cmd)))
	case "typing":
		b.sendTyping(cmd) // fire-and-forget: no ack by protocol
	default:
		b.srv.Send(ErrorFrame{Type: "error", Error: "unknown frame type: " + cmd.Type})
	}
}

func parseJID(raw string) (types.JID, error) {
	if !strings.Contains(raw, "@") {
		raw += "@s.whatsapp.net"
	}
	return types.ParseJID(raw)
}

func (b *Bridge) sendText(cmd Command) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	var msg *waE2E.Message
	if cmd.ReplyTo != "" {
		msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(cmd.Text),
			ContextInfo: &waE2E.ContextInfo{
				StanzaID:      proto.String(cmd.ReplyTo),
				Participant:   proto.String(jid.String()),
				QuotedMessage: &waE2E.Message{Conversation: proto.String("")},
			},
		}}
	} else {
		msg = &waE2E.Message{Conversation: proto.String(cmd.Text)}
	}
	_, err = b.cli.SendMessage(ctx, jid, msg)
	return err
}

func (b *Bridge) sendMedia(cmd Command) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}
	data, err := os.ReadFile(cmd.FilePath)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	kind := whatsmeow.MediaDocument
	switch {
	case strings.HasPrefix(cmd.Mimetype, "image/"):
		kind = whatsmeow.MediaImage
	case strings.HasPrefix(cmd.Mimetype, "video/"):
		kind = whatsmeow.MediaVideo
	case strings.HasPrefix(cmd.Mimetype, "audio/"):
		kind = whatsmeow.MediaAudio
	}
	up, err := b.cli.Upload(ctx, data, kind)
	if err != nil {
		return err
	}
	common := struct {
		URL, DirectPath       string
		MediaKey, SHA, EncSHA []byte
		Len                   uint64
	}{up.URL, up.DirectPath, up.MediaKey, up.FileSHA256, up.FileEncSHA256, uint64(len(data))}

	var msg *waE2E.Message
	switch kind {
	case whatsmeow.MediaImage:
		msg = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
			URL: proto.String(common.URL), DirectPath: proto.String(common.DirectPath),
			MediaKey: common.MediaKey, FileSHA256: common.SHA, FileEncSHA256: common.EncSHA,
			FileLength: proto.Uint64(common.Len), Mimetype: proto.String(cmd.Mimetype)}}
	case whatsmeow.MediaVideo:
		msg = &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
			URL: proto.String(common.URL), DirectPath: proto.String(common.DirectPath),
			MediaKey: common.MediaKey, FileSHA256: common.SHA, FileEncSHA256: common.EncSHA,
			FileLength: proto.Uint64(common.Len), Mimetype: proto.String(cmd.Mimetype)}}
	case whatsmeow.MediaAudio:
		msg = &waE2E.Message{AudioMessage: &waE2E.AudioMessage{
			URL: proto.String(common.URL), DirectPath: proto.String(common.DirectPath),
			MediaKey: common.MediaKey, FileSHA256: common.SHA, FileEncSHA256: common.EncSHA,
			FileLength: proto.Uint64(common.Len), Mimetype: proto.String(cmd.Mimetype)}}
	default:
		msg = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
			URL: proto.String(common.URL), DirectPath: proto.String(common.DirectPath),
			MediaKey: common.MediaKey, FileSHA256: common.SHA, FileEncSHA256: common.EncSHA,
			FileLength: proto.Uint64(common.Len), Mimetype: proto.String(cmd.Mimetype),
			FileName: proto.String(cmd.FileName)}}
	}
	_, err = b.cli.SendMessage(ctx, jid, msg)
	return err
}

func (b *Bridge) sendTyping(cmd Command) {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return
	}
	state := types.ChatPresenceComposing
	if cmd.State == "paused" {
		state = types.ChatPresencePaused
	}
	_ = b.cli.SendChatPresence(context.Background(), jid, state, types.ChatPresenceMediaText)
}

func (b *Bridge) RegisterEventHandlers() {
	b.cli.AddEventHandler(func(evt any) {
		switch v := evt.(type) {
		case *events.Connected:
			b.srv.SetConnected(true)
			b.srv.Send(Status{Type: "status", Status: "connected"})
		case *events.Disconnected:
			b.srv.SetConnected(false)
			b.srv.Send(Status{Type: "status", Status: "disconnected"})
		case *events.LoggedOut:
			b.srv.Send(ErrorFrame{Type: "error",
				Error: "whatsapp session logged out; run `durin channels login whatsapp`"})
			os.Exit(4)
		case *events.Message:
			b.onMessage(v)
		}
	})
}

func (b *Bridge) onMessage(v *events.Message) {
	if v.Info.IsFromMe {
		return
	}
	msg := v.Message
	content := msg.GetConversation()
	ext := msg.GetExtendedTextMessage()
	if content == "" && ext != nil {
		content = ext.GetText()
	}

	// Mentions: own JID present in the context-info mention list.
	wasMentioned := false
	var ctxInfo *waE2E.ContextInfo
	if ext != nil {
		ctxInfo = ext.GetContextInfo()
	}
	own := b.cli.Store.ID
	if ctxInfo != nil && own != nil {
		for _, m := range ctxInfo.GetMentionedJID() {
			if strings.HasPrefix(m, own.User+"@") {
				wasMentioned = true
			}
		}
	}

	// Quoted reply context.
	var quoted *Quoted
	if ctxInfo != nil && ctxInfo.GetStanzaID() != "" {
		qText := ""
		if qm := ctxInfo.GetQuotedMessage(); qm != nil {
			qText = qm.GetConversation()
			if qText == "" && qm.GetExtendedTextMessage() != nil {
				qText = qm.GetExtendedTextMessage().GetText()
			}
		}
		quoted = &Quoted{ID: ctxInfo.GetStanzaID(), Sender: ctxInfo.GetParticipant(), Text: qText}
	}

	// Media: download to mediaDir; caption becomes content when text is empty.
	var media []string
	voice := false
	type dl struct {
		msg     whatsmeow.DownloadableMessage
		ext     string
		caption string
	}
	var d *dl
	switch {
	case msg.GetImageMessage() != nil:
		d = &dl{msg.GetImageMessage(), ".jpg", msg.GetImageMessage().GetCaption()}
	case msg.GetVideoMessage() != nil:
		d = &dl{msg.GetVideoMessage(), ".mp4", msg.GetVideoMessage().GetCaption()}
	case msg.GetDocumentMessage() != nil:
		d = &dl{msg.GetDocumentMessage(), filepath.Ext(msg.GetDocumentMessage().GetFileName()), msg.GetDocumentMessage().GetCaption()}
	case msg.GetAudioMessage() != nil:
		d = &dl{msg.GetAudioMessage(), ".ogg", ""}
		voice = msg.GetAudioMessage().GetPTT()
	}
	if d != nil {
		if data, err := b.cli.Download(context.Background(), d.msg); err == nil {
			if err := os.MkdirAll(b.mediaDir, 0o700); err == nil {
				p := filepath.Join(b.mediaDir, v.Info.ID+d.ext)
				if os.WriteFile(p, data, 0o600) == nil {
					media = append(media, p)
				}
			}
		}
		if content == "" {
			content = d.caption
		}
	}

	if content == "" && len(media) == 0 {
		return // reactions, receipts-as-messages, protocol messages: nothing to relay yet
	}

	// pn = phone-style JID when whatsmeow exposes it alongside a LID sender.
	pn := ""
	sender := v.Info.Sender.String()
	if alt := v.Info.SenderAlt; !alt.IsEmpty() {
		pn = alt.String()
	}
	chat := v.Info.Chat.String()
	isGroup := v.Info.IsGroup
	// For DMs the reply target is the sender JID (legacy protocol behavior);
	// for groups it is the group JID.
	target := sender
	if isGroup {
		target = chat
	}

	b.srv.Send(Message{
		Type: "message", PN: pn, Sender: target, Content: content, ID: v.Info.ID,
		IsGroup: isGroup, WasMentioned: wasMentioned, Media: media,
		Timestamp: v.Info.Timestamp.Unix(), Voice: voice, Quoted: quoted,
	})
}
