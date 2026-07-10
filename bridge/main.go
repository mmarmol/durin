package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
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
	}
	fs := flag.NewFlagSet(mode, flag.ExitOnError)
	port := fs.Int("port", 3001, "loopback WS port")
	authDir := fs.String("auth-dir", "", "session/auth state directory (required)")
	mediaDir := fs.String("media-dir", "", "inbound media download directory")
	fs.Parse(args)
	if *authDir == "" {
		fmt.Fprintln(os.Stderr, "--auth-dir is required")
		return 2
	}

	cli, err := NewWAClient(*authDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}

	if mode == "qr" {
		return runQR(cli)
	}
	return runServe(cli, *port, *mediaDir)
}

func runQR(cli *whatsmeow.Client) int {
	if cli.Store.ID != nil {
		fmt.Println("Already paired. Use --force via `durin channels login whatsapp --force` (clears auth dir) to re-pair.")
		return 0
	}
	qrChan, _ := cli.GetQRChannel(context.Background())
	if err := cli.Connect(); err != nil {
		fmt.Fprintln(os.Stderr, "connect:", err)
		return 1
	}
	for evt := range qrChan {
		switch evt.Event {
		case "code":
			fmt.Println("Scan this QR with WhatsApp (Linked devices):")
			qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
		case "success":
			fmt.Println("Paired successfully.")
			return 0
		case "timeout":
			fmt.Fprintln(os.Stderr, "QR timed out; run login again.")
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
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
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
