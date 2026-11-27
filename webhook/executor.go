package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
)

const wasmInputDir = "/hook-input"

// Exec resolves a configured command and runs it with its full argv and environment.
type Exec interface {
	Resolve(command, dir string) (string, error)
	CombinedOutput(ctx context.Context, path string, args []string, dir string, env []string, inputDir string) ([]byte, error)
}

func hookExecutor(name string) (Exec, error) {
	switch name {
	case "", "process":
		return processExec{}, nil
	case "wasm":
		return wasmExec{}, nil
	default:
		return nil, fmt.Errorf("unknown command-executor %q", name)
	}
}

func commandPath(command, dir string) string {
	if filepath.IsAbs(command) || dir == "" {
		return command
	}
	return filepath.Join(dir, command)
}

type processExec struct{}

func (processExec) Resolve(command, dir string) (string, error) {
	return exec.LookPath(commandPath(command, dir))
}

func (processExec) CombinedOutput(ctx context.Context, path string, args []string, dir string, env []string, _ string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, path)
	cmd.Args = args
	cmd.Dir = dir
	cmd.Env = env
	return cmd.CombinedOutput()
}

type wasmExec struct{}

func (wasmExec) Resolve(command, dir string) (string, error) {
	if command == "" {
		return "", fmt.Errorf("missing WebAssembly module path")
	}
	path, err := filepath.Abs(commandPath(command, dir))
	if err != nil {
		return "", err
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("WebAssembly module %q is not a regular file", path)
	}
	return path, nil
}

func (wasmExec) CombinedOutput(ctx context.Context, path string, args []string, dir string, env []string, inputDir string) ([]byte, error) {
	// Use the server's PATH to locate Wasmtime, just as the process executor
	// resolves executables before applying request-derived environment values.
	runtimeArgs := []string{"run", "-S", "inherit-env"}
	// Only components can import WASI HTTP. Wasmtime rejects that flag for
	// core modules, including WASI Preview 1 commands.
	module, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	var header [8]byte
	_, err = io.ReadFull(module, header[:])
	closeErr := module.Close()
	if err != nil {
		return nil, fmt.Errorf("read WebAssembly header: %w", err)
	}
	if closeErr != nil {
		return nil, closeErr
	}
	if header == [8]byte{0, 'a', 's', 'm', 0x0d, 0, 1, 0} {
		runtimeArgs = append(runtimeArgs, "-S", "http")
	}
	if dir != "" {
		absoluteDir, err := filepath.Abs(dir)
		if err != nil {
			return nil, err
		}
		runtimeArgs = append(runtimeArgs, "--dir", absoluteDir+"::/work", "-S", "cwd=/work")
	}
	if inputDir != "" {
		absoluteInputDir, err := filepath.Abs(inputDir)
		if err != nil {
			return nil, err
		}
		runtimeArgs = append(runtimeArgs, "--dir", absoluteInputDir+"::"+wasmInputDir)
	}
	runtimeArgs = append(runtimeArgs, path)
	// Wasmtime sets the guest's argv[0] to the module name.
	runtimeArgs = append(runtimeArgs, args[1:]...)
	cmd := exec.CommandContext(ctx, "wasmtime", runtimeArgs...)
	cmd.Dir = dir
	cmd.Env = env
	return cmd.CombinedOutput()
}
