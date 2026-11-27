"""Opt-in live review acceptance test; no persistent service or label changes."""
import http.server, json, os, pathlib, shlex, subprocess, tarfile, tempfile, threading, traceback
import modal

app = modal.App("wrev-inkling-b200-full-pr-review")
image = modal.Image.from_registry("golang:1.22.12-bookworm", add_python="3.11")
commit = "cece1ec8f6d570513fb6d7fc9d7d743a4ff8be74"
marker = "wrev-fixture-evidence-986331"
call_id = "call_fixture_read"

def run(*cmd, cwd=None, env=None):
    subprocess.run(cmd, cwd=cwd, env=env, check=True)

def exact_edit(path, before, after):
    content = path.read_text()
    assert content.count(before) == 1, (path, before)
    path.write_text(content.replace(before, after))

test_go = r'''
package main

import (
    "crypto/hmac"
    "crypto/sha256"
    "database/sql"
    "encoding/hex"
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "os"
    "path/filepath"
    "strconv"
    "strings"
    "testing"
    "time"
)

func TestModalLabelToWasmtime(t *testing.T) {
    s := reviewTestService(t)
    repo := os.Getenv("REVIEW_REPO")
    head := os.Getenv("REVIEW_HEAD")
    number, err := strconv.ParseInt(os.Getenv("REVIEW_PR"), 10, 64)
    owner, _, ok := strings.Cut(repo, "/")
    if err != nil || !ok || number < 1 { t.Fatal("invalid pinned PR fixture") }
    s.owner = strings.ToLower(owner)
    t.Setenv("WREV_GITHUB_TOKEN", "fixture-only-never-inherited-by-wasm")
    t.Setenv("CHATGPT_ACCESS_TOKEN", "fixture-only")
    t.Setenv("OPENAI_MODEL", "fixture-model")
    if s.comments != nil { t.Fatal("GitHub writeback must be disabled") }
    bytes, err := os.ReadFile(os.Getenv("SMOKE_WRAPPER"))
    if err != nil { t.Fatal(err) }
    s.binary = filepath.Join(s.data, "fixture-runner")
    if err := os.WriteFile(s.binary, bytes, 0700); err != nil { t.Fatal(err) }

    server := httptest.NewServer(http.HandlerFunc(s.githubWebhook))
    defer server.Close()
    body, err := json.Marshal(map[string]any{
        "action": "labeled", "number": number, "label": map[string]string{"name":"wrev"},
        "repository": map[string]any{"full_name": repo, "owner": map[string]string{"login":owner}},
        "pull_request": map[string]any{"head":map[string]string{"sha":head}},
    })
    if err != nil { t.Fatal(err) }
    payload := string(body)
    mac := hmac.New(sha256.New, s.signingKey)
    mac.Write([]byte(payload))
    send := func() int {
        req, err := http.NewRequest("POST", server.URL+"/github/webhook", strings.NewReader(payload))
        if err != nil { t.Fatal(err) }
        req.Header.Set("X-GitHub-Event", "pull_request")
        req.Header.Set("X-GitHub-Delivery", "modal-test-delivery")
        req.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
        resp, err := server.Client().Do(req)
        if err != nil { t.Fatal(err) }
        defer resp.Body.Close()
        return resp.StatusCode
    }
    if got := send(); got != 202 { t.Fatalf("label: got %d", got) }
    if got := send(); got != 204 { t.Fatalf("duplicate: got %d", got) }

    go s.work()
    deadline := time.Now().Add(540*time.Second)
    for time.Now().Before(deadline) {
        var state string
        var raw sql.NullString
        if err := s.db.QueryRow("SELECT state, result FROM reviews LIMIT 1").Scan(&state, &raw); err != nil { t.Fatal(err) }
        if state == "failed" { t.Fatal("review failed; see worker diagnostic") }
        if state == "completed" {
            var result struct {
                PR struct {
                    Number int64  `json:"pr_number"`
                    Head string  `json:"head"`
                }  `json:"pr"`
                Agent string  `json:"agent"`
                Findings []json.RawMessage  `json:"findings"`
            }
            if !raw.Valid || json.Unmarshal([]byte(raw.String), &result) != nil ||
                result.PR.Number != number || result.PR.Head != head ||
                result.Agent != "wrev" || result.Findings == nil {
                t.Fatalf("bad persisted findings: %s", raw.String)
            }
            var count int
            if err := s.db.QueryRow("SELECT COUNT(*) FROM reviews").Scan(&count); err != nil || count != 1 {
                t.Fatalf("expected one review, got %d (%v)", count, err)
            }
            if err := os.WriteFile(os.Getenv("REVIEW_RESULT_PATH"), []byte(raw.String), 0600); err != nil { t.Fatal(err) }
            t.Log("signed delivery -> one queued review -> Wasmtime -> persisted findings")
            return
        }
        time.Sleep(50*time.Millisecond)
    }
    t.Fatal("review did not reach completed before deadline")
}
'''

@app.function(image=image, cpu=4, memory=4096, timeout=600, max_containers=1)
def prepare(target):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        run("git","init","-q",td)
        run("git","fetch","--quiet","--depth=1","https://github.com/r33drichards/wrev-inkling-modal.git",commit,cwd=d)
        run("git","checkout","--quiet","--detach","FETCH_HEAD",cwd=d)
        assert subprocess.check_output(["git","rev-parse","HEAD"],cwd=d,text=True).strip() == commit
        print("Fetched pinned integration revision", flush=True)
        res = d/"downloads"; res.mkdir()
        def unpack(url, flag, name):
            p = res/name
            run("curl","-fsSL","--retry","2","--max-time","120","-o",str(p),url)
            with tarfile.open(p, "r:*") as archive:
                archive.extractall(res, filter="data")
        unpack("https://github.com/bytecodealliance/wasmtime/releases/download/v51.0.0/wasmtime-v51.0.0-x86_64-linux.tar.xz","-xJf","wasmtime.tar.xz")
        unpack("https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz","-xzf","wasi-sdk.tar.gz")
        rustup = res/"rustup-init"
        run("curl","-fsSL","--retry","2","--max-time","120","-o",str(rustup),"https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init")
        rustup.chmod(0o700)
        env = dict(os.environ, CARGO_HOME=str(d/"cargo"), RUSTUP_HOME=str(d/"rustup"), WASI_SDK=str(res/"wasi-sdk-24.0-x86_64-linux"), GITHUB_TOKEN="", WREV_GITHUB_COMMENT_TOKEN="")
        env["CC_wasm32_wasip2"] = env["WASI_SDK"] + "/bin/clang"
        run(str(rustup),"-y","--no-modify-path","--profile","minimal","--default-toolchain","1.100.0","--target","wasm32-wasip2",env=env)
        wasmtime = res/"wasmtime-v51.0.0-x86_64-linux"/"wasmtime"
        wasm = d/"wrev/target/wasm32-wasip2/debug/wrev.wasm"
        exact_edit(d/"wrev/src/model.rs",
                   '    let response = crate::wasi_http::request(\n        wasi::http::types::Method::Post,\n        "api.openai.com",',
                   '    let authority = std::env::var("WREV_SMOKE_LOOPBACK")?;\n    #[cfg(all(target_os = "wasi", target_env = "p2"))]\n    let response = crate::wasi_http::request(\n        wasi::http::types::Method::Post,\n        &authority,')
        exact_edit(d/"wrev/src/wasi_http.rs",'    request\n        .set_scheme(Some(&Scheme::Https))',
                   '    ensure!(authority.split_once(\':\').is_some_and(|(host, port)| host == "127.0.0.1" && port.parse::<u16>().is_ok()), "fixture model must be loopback");\n    request\n        .set_scheme(Some(&Scheme::Http))')
        print("Building Wasmtime component, test-only loopback transport", flush=True)
        run(str(d/"cargo/bin/cargo"),"build","--locked","--target","wasm32-wasip2","--bin","wrev",cwd=d/"wrev",env=env)
        assert wasm.is_file()
        run(str(wasmtime),"--version")
        (d/"webhook/modal_integration_test.go").write_text(test_go)
        binary=d/"webhook.test"
        run("go","test","-mod=vendor","-c","-o",str(binary),".",cwd=d/"webhook",env=env)
        return wasm.read_bytes(),wasmtime.read_bytes(),binary.read_bytes(),prepare_evidence(d,target)

import json, os, pathlib, re, signal, subprocess, tempfile, time, urllib.error, urllib.request
import modal


digest = "sha256:f5aca5f2081c8a7b9cadb046bd59c02dc3dcf214dd3254f9154a099ce9505b1a"
gpu_image = modal.Image.from_registry("vllm/vllm-openai@" + digest, add_python="3.12").entrypoint([])
model = "thinkingmachines/Inkling-2-Mini"
revision = "c9d028302cd0f035fdf8e7e91f089a4ce94d2195"
symbol = "INKLING_TOOL_ROUND_TRIP_8241"

def post(port, path, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"})
    return urllib.request.urlopen(req, timeout=185)

def read_sse(response):
    assert response.headers.get_content_type() == "text/event-stream", response.headers.get_content_type()
    events = []
    chunks = []
    for line in response:
        line = line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            chunks.append(line[5:].lstrip())
        elif not line and chunks:
            data = "\n".join(chunks); chunks.clear()
            if data != "[DONE]":
                events.append(json.loads(data))
    return events

def output(events):
    completed = [e for e in events if e.get("type") == "response.completed"]
    if len(completed) != 1:
        raise RuntimeError("stream lacks exactly one response.completed; event types: "+repr([e.get("type") for e in events][-12:]))
    return completed[0]["response"]["output"]

def spec_metrics(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics",timeout=5) as resp:
            lines=resp.read().decode().splitlines()
        result={}
        for line in lines:
            if line.startswith("#") or not re.search(r"spec_decode.*(?:draft|accept).*_total(?:\{| )",line):
                continue
            identifier,value=line.rsplit(None,1)
            key=identifier.split("{",1)[0]
            result[key]=result.get(key,0)+float(value)
        return result
    except Exception as exc:
        print("No speculation metrics:",repr(exc),flush=True)
        return {}

@app.function(image=gpu_image, gpu="B200", cpu=4, memory=16384, timeout=1500, max_containers=1)
def run_probe(artifact,target):
    import socket
    start = time.monotonic()
    run("nvidia-smi","--query-gpu=name,driver_version,memory.total","--format=csv,noheader")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory() as d:
        logfile = pathlib.Path(d)/"vllm.log"
        env = dict(os.environ, HF_HOME=str(pathlib.Path(d)/"hf"), HF_HUB_DISABLE_TELEMETRY="1", VLLM_NO_USAGE_STATS="1")
        env.pop("HF_TOKEN",None)
        env.pop("HUGGING_FACE_HUB_TOKEN",None)
        with logfile.open("w") as f:
            proc = subprocess.Popen(["vllm","serve",model,"--revision",revision,
                "--host","127.0.0.1","--port",str(port),"--tensor-parallel-size","1",
                "--language-model-only",
                "--reasoning-parser","inkling","--tool-call-parser","inkling","--enable-auto-tool-choice",
                "--speculative-config",json.dumps({"method":"dspark","num_speculative_tokens":7,"draft_sample_method":"probabilistic"})],
                env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while time.monotonic()-start < 880:
                    if proc.poll() is not None:
                        raise RuntimeError("vLLM exited during startup")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as resp:
                            assert model in {x["id"] for x in json.load(resp)["data"]}
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(2)
                else:
                    raise RuntimeError("startup exceeded 880 seconds")
                minutes = round((time.monotonic()-start)/60,2)
                print(f"READY: B200 TP1 served the pinned Inkling checkpoint after {minutes} minutes", flush=True)
                before=spec_metrics(port)
                try:
                    result=live_review(d,artifact,port,target)
                    return result
                finally:
                    after=spec_metrics(port)
                    print("DSpark counter deltas:",{k:after[k]-before.get(k,0) for k in after},flush=True)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                log=logfile.read_text(errors="replace").splitlines()
                hits=[l for l in log if re.search("nvfp4|mxfp8|modelopt|GPU.*memory|cuda graphic|[Ee]rror|weights",l,re.I)]
                print("\n".join(hits[-18:] or log[-18:]),flush=True)



def prepare_evidence(d,target):
    import io
    review_repo,review_base,review_head=(target[k] for k in ("nwo","base","head"))
    repo = d/"review-source"
    repo.mkdir()
    run("git","init","-q",cwd=repo)
    run("git","fetch","--quiet","--no-tags","--depth=12","https://github.com/"+review_repo+".git",review_base,review_head,cwd=repo)
    def git(*args):
        return subprocess.check_output(["git","--no-pager",*args],cwd=repo)
    merge_base=git("merge-base",review_base,review_head).decode().strip()
    evidence = d/"evidence-packed"; evidence.mkdir()
    for name,ref in [("repo",review_head),("base",review_base)]:
        folder=evidence/name; folder.mkdir()
        with tarfile.open(fileobj=io.BytesIO(git("archive",ref)),mode="r:") as archive:
            archive.extractall(folder,filter="data")
    pr=evidence/"pr"; pr.mkdir()
    req=target
    (pr/"pr.json").write_text(json.dumps(req))
    patch=git("diff","--no-ext-diff","--no-textconv","--binary",merge_base,review_head,"--")
    assert patch.startswith(b"diff --git ") and len(patch)<2_000_000, "empty diff or acceptance-test diff above 2 MB"
    (pr/"diff.patch").write_bytes(patch)
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode="w:gz",compresslevel=5) as t:
        for name in ("repo","base","pr"): t.add(evidence/name,arcname=name)
    print("Pinned and staged public PR evidence; no PR code executed.",flush=True)
    return data.getvalue()

class LiveProxy(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        with self.server.step_lock:
            self.process_turn()

    def process_turn(self):
        sent=False
        try:
            assert self.path=="/v1/responses" and self.headers["Authorization"]=="Bearer fixture-only"
            assert self.server.turns<20,"test permits at most 20 model turns"
            max_body=16*1024*1024
            if self.headers.get("Transfer-Encoding","").lower()=="chunked":
                buf=bytearray()
                while True:
                    size=int(self.rfile.readline().split(b";",1)[0],16)
                    if not size: break
                    assert 0<size<=max_body and len(buf)+size<=max_body,"test proxy request exceeded 16 MiB"
                    buf.extend(self.rfile.read(size))
                    assert self.rfile.read(2)==b"\r\n"
            else:
                size=int(self.headers["Content-Length"])
                assert 0<size<=max_body
                buf=self.rfile.read(size)
            req=json.loads(buf)
            assert req["model"]==model and req["stream"] is True and req["store"] is False
            got={x.get("call_id"):x.get("output") for x in req["input"] if x.get("type")=="function_call_output"}
            if self.server.expected:
                assert self.server.expected.issubset(got), "missing returned tool-call IDs"
                for call in self.server.expected:
                    body = str(got[call])
                    if "diff --git " in body:
                        self.server.read_diff=True
                self.server.outputs.update(got)
            req["max_output_tokens"]=8192
            req["temperature"]=0
            started=time.monotonic()
            with post(self.server.model_port,"/v1/responses",req) as resp:
                assert resp.headers.get_content_type()=="text/event-stream"
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                sent=True
                framed=bytearray()
                for line in resp:
                    assert len(framed)+len(line)<=8*1024*1024
                    framed.extend(line)
                    self.wfile.write(line); self.wfile.flush()
                frame=[json.loads(x[6:]) for x in framed.splitlines() if x.startswith(b"data: {")]
                items=output(frame)
                calls=[x for x in items if x.get("type")=="function_call"]
                assert all(c.get("name")=="run_js" and c.get("call_id") for c in calls),"unexpected tool"
                self.server.expected={c["call_id"] for c in calls}
                self.server.turns+=1
                usage=next((x["response"].get("usage") for x in frame if x.get("type")=="response.completed"),None)
                summary={"turn":self.server.turns,"items":[x.get("type") for x in items],"calls":len(calls),"elapsed_secs":round(time.monotonic()-started,2)}
                if usage: summary["tokens"]={k:usage.get(k) for k in ("input_tokens","output_tokens","total_tokens")}
                print("Model step:",summary,flush=True)
        except Exception as e:
            reason=repr(e)
            if isinstance(e,urllib.error.HTTPError): reason+=": "+e.read(1200).decode(errors="replace")
            self.server.errors.append(reason); print("Live Responses contract: "+reason,flush=True)
            if not sent: self.send_error(502,"Live Responses contract failed")

def live_review(d, artifact, port, target):
    review_repo,review_number,review_base,review_head=(target[k] for k in ("nwo","pr_number","base","head"))
    import io
    d=pathlib.Path(d)
    wasm_b,engine_b,go_b,evidence_b=artifact
    wasm=d/"review.wasm"; wasm.write_bytes(wasm_b)
    wasmtime=d/"wasmtime"; wasmtime.write_bytes(engine_b); wasmtime.chmod(0o700)
    worker=d/"webhook.test"; worker.write_bytes(go_b); worker.chmod(0o700)
    review=d/"evidence"; review.mkdir()
    with tarfile.open(fileobj=io.BytesIO(evidence_b),mode="r:gz") as archive:
        archive.extractall(review,filter="data")
    inputs=d/"input"; inputs.mkdir(); (inputs/"request.json").write_bytes((review/"pr/pr.json").read_bytes())
    result_path=d/"review-result.json"
    with http.server.ThreadingHTTPServer(("127.0.0.1",0),LiveProxy) as server:
        server.turns=0; server.errors=[]; server.model_port=port; server.step_lock=threading.Lock(); server.expected=set(); server.outputs={}; server.read_diff=False
        threading.Thread(target=server.serve_forever,daemon=True).start()
        wrapper=d/"wrapper.sh"
        args=[str(wasmtime),"run","-S","http","--dir",str(review)+"::/work","--dir",str(inputs)+"::/input",
              "--env","WREV_SMOKE_LOOPBACK=127.0.0.1:"+str(server.server_port),
              "--env","OPENAI_MODEL="+model,"--env","CHATGPT_ACCESS_TOKEN=fixture-only",
              "--env","WREV_MODEL_TIMEOUT_SECS=180",
              "--env","WREV_SCRIPT_MEMORY_BYTES=104857600","--env","WREV_SCRIPT_OUTPUT_BYTES=104857600",
              str(wasm),"review","/input/request.json"]
        wrapper.write_text("#!/bin/sh\nset -eu\n[ \"$1\" = review ] && [ \"$2\" = "+shlex.quote(review_repo)+" ] && [ \"$3\" = "+str(review_number)+" ] || exit 45\nexec env -i PATH=/usr/local/bin:/usr/bin:/bin "+shlex.join(args)+"\n")
        try:
            result=subprocess.run([str(worker),"-test.v","-test.run","^TestModalLabelToWasmtime$"],env=dict(os.environ,SMOKE_WRAPPER=str(wrapper),REVIEW_RESULT_PATH=str(result_path), REVIEW_REPO=review_repo, REVIEW_PR=str(review_number), REVIEW_HEAD=review_head, WREV_GITHUB_COMMENT_TOKEN=""),timeout=565)
        finally:
            server.shutdown()
        assert result.returncode==0 and server.read_diff and server.turns>=2 and not server.errors, (result.returncode,server.turns,server.read_diff,server.errors)
        data=json.loads(result_path.read_text())
        assert data["pr"]["head"]==review_head and data["pr"]["base"]==review_base and isinstance(data["findings"],list)
        for f in data["findings"]:
            assert isinstance(f.get("file"), str) and type(f.get("start_line")) is int and type(f.get("end_line")) is int and 1<=f["start_line"]<=f["end_line"] and isinstance(f.get("message"),str) and f["message"].strip(), "malformed model finding"
        summary={"model_turns":server.turns,"tool_calls":len(server.outputs),"read_diff":server.read_diff}
        print("PASS: real pinned PR was read and review JSON was persisted",summary,flush=True)
        return {"review":data,**summary}



def github_json(path, *, method="GET", body=None):
    """Use gh locally; the GPU subprocess receives no GitHub credential."""
    cmd=["gh","api","--hostname","github.com","--method",method,path]
    if body is not None:
        cmd+=["--input","-"]
    result=subprocess.run(cmd,input=json.dumps(body) if body is not None else None,
                          text=True,capture_output=True,check=True,timeout=25)
    return json.loads(result.stdout)

def assert_current(target, *, authenticated=False):
    if authenticated:
        b=github_json(f"repos/{target['nwo']}/pulls/{target['pr_number']}")
    else:
        url=f"https://api.github.com/repos/{target['nwo']}/pulls/{target['pr_number']}"
        with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"wrev-modal-public-review"}),timeout=20) as response:
            b=json.load(response)
    if b.get("state") != "open":
        raise RuntimeError("PR is no longer open")
    if (b["base"]["sha"], b["head"]["sha"]) != (target["base"], target["head"]):
        raise RuntimeError("PR base or head changed")

def public_target(repo, pr):
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo) or not 0 < pr < 2**31 or repo.split("/")[1] in {".", ".."}:
        raise ValueError("invalid PR identity")
    url=f"https://api.github.com/repos/{repo}/pulls/{pr}"
    req=urllib.request.Request(url,headers={"User-Agent":"wrev-modal-public-review"})
    with urllib.request.urlopen(req,timeout=20) as response:
        x=json.load(response)
    if x.get("state") != "open" or x["base"]["repo"]["private"] is not False:
        raise ValueError("expected an open public PR")
    if x["base"]["repo"]["full_name"].lower() != repo.lower() or not all(re.fullmatch(r"[a-f0-9]{40}", x[k]["sha"]) for k in ("base", "head")):
        raise ValueError("unexpected repository or invalid pinned revisions")
    return {"nwo":x["base"]["repo"]["full_name"],"pr_number":pr,"base":x["base"]["sha"],"head":x["head"]["sha"],
            "title":x["title"],"body":x.get("body")}

def render_result(target,result):
    lines=[f"**Wrev review completed** for `{target['head'][:7]}` using Inkling 2 Mini in Wasmtime on Modal.",
           f"{result['model_turns']} model turns, {result['tool_calls']} tool calls."]
    findings=result["review"]["findings"]
    if not findings:
        lines+=["","No actionable findings reported."]
    else:
        lines+=["","Findings:"]
        for f in findings[:25]:
            # Quote as plain text and avoid triggering GitHub mentions.
            path=f["file"].replace("`","'").replace("@","＠").replace("\n"," ")
            safe=" ".join(f["message"].split()).replace("@","＠").replace("<","&lt;").replace(">","&gt;")
            safe=re.sub(r"([\\`*_\[\]])",r"\\\1",safe)[:1600]
            lines.append(f"- `{path}:{f['start_line']}-{f['end_line']}`: {safe}")
        if len(findings)>25:
            lines.append(f"- {len(findings)-25} additional findings in the test output.")
    return "\n".join(lines)

@app.local_entrypoint()
def main(repo: str, pr: int, comment: bool=False):
    target=public_target(repo,pr)
    comment_id=None
    if comment:
        assert_current(target,authenticated=True)
    artifact=prepare.remote(target)
    try:
        assert_current(target,authenticated=comment)
        if comment:
            posted=github_json(f"repos/{target['nwo']}/issues/{pr}/comments",method="POST",
                               body={"body":f"**Wrev review started** for `{target['head'][:7]}` using Inkling 2 Mini in Wasmtime on Modal. This comment will be updated with the result."})
            comment_id=posted["id"]
            print("Started:",posted["html_url"],flush=True)
        print("Pinned PR and binaries ready; starting one B200.",flush=True)
        result=run_probe.remote(artifact,target)
        print("WREV_REVIEW_RESULT "+json.dumps(result,ensure_ascii=False),flush=True)
        if comment_id is not None:
            assert_current(target,authenticated=True)
            updated=github_json(f"repos/{target['nwo']}/issues/comments/{comment_id}",method="PATCH",
                                body={"body":render_result(target,result)})
            print("Updated:",updated["html_url"],flush=True)
    except (Exception, KeyboardInterrupt):
        if comment_id is not None:
            try:
                github_json(f"repos/{target['nwo']}/issues/comments/{comment_id}",method="PATCH",
                            body={"body":f"**Wrev review did not publish findings** for `{target['head'][:7]}`. The run was interrupted, failed, or the PR revision changed."})
            except Exception:
                print(f"Could not update the existing start comment ({comment_id}); not creating a new comment.",flush=True)
        raise
