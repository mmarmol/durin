package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
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
	// out decouples inbound message relay from whatsmeow's event dispatch:
	// a stalled WS client (bounded by the server's write deadline) must never
	// delay the WhatsApp event loop.
	out chan any
}

func NewBridge(cli *whatsmeow.Client, srv *Server, mediaDir string) *Bridge {
	return &Bridge{cli: cli, srv: srv, mediaDir: mediaDir, out: make(chan any, 256)}
}

// enqueue hands a frame to the outbound consumer goroutine without ever
// blocking the caller; when the buffer is full the frame is dropped.
func (b *Bridge) enqueue(frame any) {
	select {
	case b.out <- frame:
	default:
		fmt.Fprintln(os.Stderr, "outbound buffer full; dropping frame")
	}
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

// resolveReplyParticipant picks the ContextInfo.Participant for a reply. In
// DMs the destination JID is itself the participant. In groups the
// destination JID is the group, never a participant, so there is no safe
// default: the caller-supplied participant (learned from the inbound
// message being replied to) is used as-is, which may be empty — callers
// must treat an empty result as "no quote".
func resolveReplyParticipant(known string, jid types.JID) string {
	if known != "" || jid.Server == types.GroupServer {
		return known
	}
	return jid.String()
}

// buildTextMessage constructs the outbound message. A quote is only
// attached when both a stanza ID and a participant are known; otherwise a
// plain text message is sent rather than a quote with a wrong Participant.
func buildTextMessage(text, replyTo, participant string) *waE2E.Message {
	if replyTo != "" && participant != "" {
		return &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(text),
			ContextInfo: &waE2E.ContextInfo{
				StanzaID:      proto.String(replyTo),
				Participant:   proto.String(participant),
				QuotedMessage: &waE2E.Message{Conversation: proto.String("")},
			},
		}}
	}
	return &waE2E.Message{Conversation: proto.String(text)}
}

func (b *Bridge) sendText(cmd Command) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	participant := resolveReplyParticipant(cmd.ReplyToParticipant, jid)
	msg := buildTextMessage(cmd.Text, cmd.ReplyTo, participant)
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
	go func() {
		for frame := range b.out {
			_ = b.srv.Send(frame)
		}
	}()
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

// sanitizeMediaName turns a WhatsApp stanza ID (attacker-controlled: it
// arrives in an inbound message) into a filesystem-safe base name, so it can
// never be used for path traversal or to escape the media directory. Only
// [A-Za-z0-9._-] survive; everything else becomes "_". If nothing survives,
// a random name is generated instead of writing to a fixed/guessable path.
func sanitizeMediaName(id, ext string) string {
	var sb strings.Builder
	for _, r := range id {
		switch {
		case r >= 'A' && r <= 'Z', r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '.', r == '_', r == '-':
			sb.WriteRune(r)
		default:
			sb.WriteRune('_')
		}
	}
	name := sb.String()
	if name == "" {
		buf := make([]byte, 8)
		if _, err := rand.Read(buf); err != nil {
			name = "media"
		} else {
			name = hex.EncodeToString(buf)
		}
	}
	return name + ext
}

// mentionMatchesOwnUser reports whether a mentioned JID's user part refers
// to this device, under either its phone identity or its LID identity. An
// empty own identity (device not yet known, or no LID assigned) never
// matches, and an empty mentioned user never matches.
func mentionMatchesOwnUser(mentionedUser, ownPhoneUser, ownLIDUser string) bool {
	if mentionedUser == "" {
		return false
	}
	return (ownPhoneUser != "" && mentionedUser == ownPhoneUser) ||
		(ownLIDUser != "" && mentionedUser == ownLIDUser)
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

	// Context info lives on whichever message part is present: extended text
	// for plain replies/mentions, or the media message for captioned media
	// (a photo whose caption @mentions us, a media reply, ...).
	var ctxInfo *waE2E.ContextInfo
	switch {
	case ext != nil:
		ctxInfo = ext.GetContextInfo()
	case msg.GetImageMessage() != nil:
		ctxInfo = msg.GetImageMessage().GetContextInfo()
	case msg.GetVideoMessage() != nil:
		ctxInfo = msg.GetVideoMessage().GetContextInfo()
	case msg.GetDocumentMessage() != nil:
		ctxInfo = msg.GetDocumentMessage().GetContextInfo()
	case msg.GetAudioMessage() != nil:
		ctxInfo = msg.GetAudioMessage().GetContextInfo()
	}

	// Mentions: own user present in the context-info mention list. Compare
	// parsed JID users so device-suffixed mention JIDs still match. LID
	// groups carry LID JIDs in MentionedJID, not phone JIDs, so both of
	// this device's identities are checked.
	wasMentioned := false
	if ctxInfo != nil {
		var ownUser, ownLIDUser string
		if b.cli.Store.ID != nil {
			ownUser = b.cli.Store.ID.User
		}
		if !b.cli.Store.LID.IsEmpty() {
			ownLIDUser = b.cli.Store.LID.User
		}
		for _, m := range ctxInfo.GetMentionedJID() {
			if j, err := types.ParseJID(m); err == nil && mentionMatchesOwnUser(j.User, ownUser, ownLIDUser) {
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
		data, err := b.cli.Download(context.Background(), d.msg)
		if err != nil {
			fmt.Fprintf(os.Stderr, "media download failed for %s: %v\n", v.Info.ID, err)
		} else if err := os.MkdirAll(b.mediaDir, 0o700); err == nil {
			p := filepath.Join(b.mediaDir, sanitizeMediaName(v.Info.ID, d.ext))
			// Defense in depth: sanitizeMediaName already strips path
			// separators, but verify containment before ever writing.
			if rel, relErr := filepath.Rel(b.mediaDir, p); relErr != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
				fmt.Fprintf(os.Stderr, "media path %q escapes media dir %q; skipping write\n", p, b.mediaDir)
			} else if os.WriteFile(p, data, 0o600) == nil {
				media = append(media, p)
			}
		}
		if content == "" {
			content = d.caption
		}
		// A failed download must not drop the message: ship a placeholder so
		// the frame (including voice:true for PTT) still reaches the adapter.
		if content == "" && len(media) == 0 {
			content = "[media could not be downloaded]"
		}
	}

	if content == "" && len(media) == 0 {
		return // reactions, receipts-as-messages, protocol messages: nothing to relay yet
	}

	// Wire contract: `sender` is the reply target — the group JID for group
	// messages, the participant JID for DMs. `pn` carries the other identity:
	// in groups the real participant JID (phone-form preferred when the
	// primary sender is a LID), in DMs the sender's alternative form.
	isGroup := v.Info.IsGroup
	var target, pn string
	if isGroup {
		target = v.Info.Chat.String()
		pn = v.Info.Sender.String()
		if v.Info.Sender.Server == types.HiddenUserServer &&
			v.Info.SenderAlt.Server == types.DefaultUserServer {
			pn = v.Info.SenderAlt.String()
		}
	} else {
		target = v.Info.Sender.String()
		if !v.Info.SenderAlt.IsEmpty() {
			pn = v.Info.SenderAlt.String()
		}
	}

	b.enqueue(Message{
		Type: "message", PN: pn, Sender: target, Content: content, ID: v.Info.ID,
		IsGroup: isGroup, WasMentioned: wasMentioned, Media: media,
		Timestamp: v.Info.Timestamp.Unix(), Voice: voice, Quoted: quoted,
	})
}
