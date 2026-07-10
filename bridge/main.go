package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
)

// version is injected at build time: -ldflags "-X main.version=..."
var version = "dev"

func main() {
	os.Exit(run())
}

func run() int {
	if len(os.Args) > 1 && os.Args[1] == "--version" {
		fmt.Println(version)
		return 0
	}
	mode := "serve"
	args := os.Args[1:]
	if len(args) > 0 && (args[0] == "serve" || args[0] == "qr") {
		mode, args = args[0], args[1:]
	} else if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		fmt.Fprintf(os.Stderr, "unknown mode: %q (want serve or qr)\n", args[0])
		return 2
	}
	fs := flag.NewFlagSet(mode, flag.ExitOnError)
	port := fs.Int("port", 3001, "loopback WS port")
	authDir := fs.String("auth-dir", "", "session/auth state directory (required)")
	mediaDir := fs.String("media-dir", "", "inbound media download directory")
	emitFrames := fs.Bool("emit-frames", false,
		"qr mode: emit NDJSON qr/status frames on stdout instead of an ASCII QR")
	fs.Parse(args)
	if *authDir == "" {
		fmt.Fprintln(os.Stderr, "--auth-dir is required")
		return 2
	}

	// In qr --emit-frames mode stdout is an NDJSON channel; silence the
	// whatsmeow client logger so it can't corrupt the frame stream.
	cli, err := NewWAClient(*authDir, mode == "qr" && *emitFrames)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}

	if mode == "qr" {
		return runQR(cli, *emitFrames)
	}
	return runServe(cli, *port, *mediaDir)
}

// emitFrame writes one NDJSON frame to stdout for the gateway to parse.
func emitFrame(v any) {
	if b, err := json.Marshal(v); err == nil {
		fmt.Println(string(b))
	}
}

// runQR drives QR-code pairing. With emitFrames, it streams machine-readable
// qr/status frames on stdout (consumed by the gateway to show the QR in the
// webui); otherwise it renders a scannable ASCII QR for the terminal (the CLI
// `durin channels login whatsapp` path).
func runQR(cli *whatsmeow.Client, emitFrames bool) int {
	if cli.Store.ID != nil {
		if emitFrames {
			emitFrame(Status{Type: "status", Status: "already_paired"})
		} else {
			fmt.Println("Already paired. Use --force via `durin channels login whatsapp --force` (clears auth dir) to re-pair.")
		}
		return 0
	}
	qrChan, _ := cli.GetQRChannel(context.Background())
	if err := cli.Connect(); err != nil {
		if emitFrames {
			emitFrame(ErrorFrame{Type: "error", Error: err.Error()})
		} else {
			fmt.Fprintln(os.Stderr, "connect:", err)
		}
		return 1
	}
	for evt := range qrChan {
		switch evt.Event {
		case "code":
			if emitFrames {
				emitFrame(QR{Type: "qr", Code: evt.Code})
			} else {
				fmt.Println("Scan this QR with WhatsApp (Linked devices):")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			}
		case "success":
			if emitFrames {
				emitFrame(Status{Type: "status", Status: "connected"})
			} else {
				fmt.Println("Paired successfully.")
			}
			return 0
		case "timeout":
			if emitFrames {
				emitFrame(Status{Type: "status", Status: "timeout"})
			} else {
				fmt.Fprintln(os.Stderr, "QR timed out; run login again.")
			}
			return 1
		}
	}
	return 1
}

func runServe(cli *whatsmeow.Client, port int, mediaDir string) int {
	token := os.Getenv("BRIDGE_TOKEN")
	if token == "" {
		fmt.Fprintln(os.Stderr, "BRIDGE_TOKEN env var is required in serve mode")
		return 2
	}
	if cli.Store.ID == nil {
		fmt.Fprintln(os.Stderr, "no paired WhatsApp session; run `durin channels login whatsapp`")
		return 3
	}
	srv := NewServer(token, version)
	bridge := NewBridge(cli, srv, mediaDir)
	srv.OnCommand = bridge.HandleCommand
	bridge.RegisterEventHandlers()

	if err := cli.Connect(); err != nil {
		fmt.Fprintln(os.Stderr, "connect:", err)
		return 1
	}

	httpSrv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port), Handler: srv.Handler()}
	// Register the signal handler before spawning the goroutine: a signal
	// delivered before a late Notify would kill the process with the default
	// disposition, skipping the clean disconnect and server close.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		cli.Disconnect()
		httpSrv.Close()
	}()
	if err := httpSrv.ListenAndServe(); err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "ws server:", err)
		return 1
	}
	return 0
}
