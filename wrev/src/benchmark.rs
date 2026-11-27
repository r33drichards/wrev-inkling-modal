use crate::{config, model, review};
use anyhow::{Context, Result, ensure};
use minimal_mcp_agent::quickjs_tools::QuickJsTools;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
#[cfg(not(target_os = "wasi"))]
use std::process::Command;
use std::{
    env, fs,
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use tokio::runtime::Runtime;

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct Snapshot {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) repo: Option<String>,
    pub(crate) nwo: String,
    pub(crate) pr_number: u64,
    pub(crate) base: String,
    pub(crate) head: String,
    pub(crate) title: Option<String>,
    pub(crate) body: Option<String>,
}

#[cfg(not(target_os = "wasi"))]
fn git(repo: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let output = Command::new("git")
        .args(["--no-pager", "--literal-pathspecs", "-C"])
        .arg(repo)
        .args(args)
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()?;
    ensure!(
        output.status.success(),
        "Could not read pinned checkout using git {}",
        args[0]
    );
    Ok(output.stdout)
}

fn path(args: &Map<String, Value>) -> Result<&str> {
    let path = args
        .get("path")
        .and_then(Value::as_str)
        .context("Missing path")?;
    let path = path
        .strip_prefix("/work/")
        .context("Path must begin with /work/")?;
    ensure!(
        path.len() <= 4096
            && path
                .split('/')
                .all(|part| { !part.is_empty() && part != "." && part != ".." })
            && !path.contains(['\0', '\\', ':'])
            && !path
                .split('/')
                .any(|part| part.eq_ignore_ascii_case(".git")),
        "Invalid snapshot path"
    );
    ensure!(
        matches!(
            path,
            "repo" | "base" | "pr" | "pr/pr.json" | "pr/diff.patch"
        ) || path.starts_with("repo/")
            || path.starts_with("base/"),
        "Path is outside review evidence"
    );
    Ok(path)
}

fn lines(data: &[u8], args: &Map<String, Value>) -> Result<Value> {
    let start = args
        .get("start_line")
        .map_or(Some(1), Value::as_u64)
        .filter(|&line| line > 0)
        .context("Invalid start_line")?;
    let skip = usize::try_from(start - 1)?;
    let max_lines = args
        .get("max_lines")
        .map_or(Some(200), Value::as_u64)
        .filter(|limit| (1..=200).contains(limit))
        .context("Invalid max_lines")?;
    let text = String::from_utf8_lossy(data);
    let mut result = String::new();
    let mut count = 0;
    for line in text.lines().skip(skip).take(max_lines as usize) {
        if count > 0 && result.len() + line.len() > 20_000 {
            break;
        }
        let mut end = 20_000.min(line.len());
        while !line.is_char_boundary(end) {
            end -= 1;
        }
        result.push_str(&line[..end]);
        if end < line.len() {
            result.push_str(" [line truncated]");
        }
        result.push('\n');
        count += 1;
    }
    Ok(
        json!({"start_line": start, "text": result, "next_line": if count > 0 && text.lines().nth(skip.saturating_add(count)).is_some() { Some(start + count as u64) } else { None }}),
    )
}

#[cfg(not(target_os = "wasi"))]
pub fn run() -> Result<()> {
    let mut args = env::args_os().skip(2);
    let checkout = PathBuf::from(args.next().context("Missing checkout path")?);
    let metadata = args.next().context("Missing pull request JSON")?;
    ensure!(args.next().is_none(), "Unexpected arguments");
    let snapshot: Snapshot = serde_json::from_slice(&fs::read(metadata)?)?;
    println!(
        "{}",
        review(
            &snapshot,
            || temporary_mount(&checkout, &snapshot),
            config::chatgpt_token
        )?
    );
    Ok(())
}

pub(crate) struct Mount {
    root: PathBuf,
    #[cfg(not(target_os = "wasi"))]
    _temp: Option<tempfile::TempDir>,
}

impl Mount {
    pub(crate) fn supplied(root: PathBuf) -> Self {
        Self {
            root,
            #[cfg(not(target_os = "wasi"))]
            _temp: None,
        }
    }
}

fn regular_path(root: &Path, relative: &str, directory: bool) -> Result<PathBuf> {
    let mut candidate = root.to_owned();
    ensure!(
        fs::symlink_metadata(root)?.is_dir(),
        "Invalid snapshot root"
    );
    let mut components = relative.split('/').peekable();
    while let Some(component) = components.next() {
        candidate.push(component);
        let kind = fs::symlink_metadata(&candidate)?.file_type();
        if components.peek().is_some() || directory {
            ensure!(kind.is_dir(), "Snapshot path is not a directory");
        } else {
            ensure!(kind.is_file(), "Snapshot path is not a regular file");
        }
    }
    Ok(candidate)
}

fn read(root: &Path, relative: &str, limit: u64) -> Result<Vec<u8>> {
    let file = fs::File::open(regular_path(root, relative, false)?)?;
    ensure!(
        file.metadata()?.len() <= limit,
        "Snapshot file is too large"
    );
    let mut data = Vec::new();
    file.take(limit + 1).read_to_end(&mut data)?;
    ensure!(data.len() as u64 <= limit, "Snapshot file is too large");
    Ok(data)
}

pub(crate) fn metadata(root: &Path) -> Result<Snapshot> {
    let snapshot: Snapshot = serde_json::from_slice(&read(root, "pr/pr.json", 2_000_000)?)?;
    snapshot.validate()?;
    Ok(snapshot)
}

impl Snapshot {
    fn validate(&self) -> Result<()> {
        ensure!(
            review::allowed_repo(&self.nwo)
                && self.pr_number > 0
                && review::valid_sha(&self.base)
                && review::valid_sha(&self.head),
            "Invalid review snapshot metadata"
        );
        Ok(())
    }
}

fn load_request(path: &Path) -> Result<Snapshot> {
    let mut data = Vec::new();
    fs::File::open(path)?
        .take(2_000_001)
        .read_to_end(&mut data)?;
    ensure!(data.len() <= 2_000_000, "Review request is too large");
    let snapshot: Snapshot = serde_json::from_slice(&data)?;
    snapshot.validate()?;
    Ok(snapshot)
}

pub fn run_mounted() -> Result<()> {
    let mut args = env::args_os().skip(2);
    let request = args
        .next()
        .context("Usage: wrev review-snapshot REQUEST_JSON [MOUNT_ROOT]")?;
    let root = args
        .next()
        .map_or_else(|| PathBuf::from("/work"), PathBuf::from);
    ensure!(args.next().is_none(), "Unexpected arguments");
    let snapshot = load_request(Path::new(&request))?;
    ensure!(
        metadata(&root)? == snapshot,
        "Mounted snapshot does not match the review request"
    );
    println!(
        "{}",
        review(
            &snapshot,
            || Ok(Mount::supplied(root)),
            config::chatgpt_token
        )?
    );
    Ok(())
}

#[cfg(not(target_os = "wasi"))]
pub fn prepare() -> Result<()> {
    let mut args = env::args_os().skip(2);
    let request = args
        .next()
        .context("Usage: wrev prepare-review REQUEST_JSON MOUNT_ROOT")?;
    let root = PathBuf::from(args.next().context("Missing mount root")?);
    ensure!(
        args.next().is_none() && !root.exists(),
        "Expected a new mount directory"
    );
    let snapshot = load_request(Path::new(&request))?;
    let checkout = fetch(&snapshot, &config::required("WREV_GITHUB_TOKEN")?)?;
    let parent = root
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let staged = tempfile::tempdir_in(parent)?;
    seed(checkout.path(), &snapshot, staged.path())?;
    fs::rename(staged.path(), root)?;
    Ok(())
}

#[cfg(not(target_os = "wasi"))]
pub(crate) fn temporary_mount(checkout: &Path, snapshot: &Snapshot) -> Result<Mount> {
    let temp = tempfile::tempdir()?;
    seed(checkout, snapshot, temp.path())?;
    Ok(Mount {
        root: temp.path().to_owned(),
        _temp: Some(temp),
    })
}

#[cfg(not(target_os = "wasi"))]
fn seed(checkout: &Path, snapshot: &Snapshot, root: &Path) -> Result<()> {
    snapshot.validate()?;
    let pr = root.join("pr");
    fs::create_dir(&pr)?;
    fs::create_dir(root.join("out"))?;
    let index = tempfile::tempdir()?;
    for (directory, reference) in [("repo", &snapshot.head), ("base", &snapshot.base)] {
        let destination = root.join(directory);
        fs::create_dir(&destination)?;
        let run = |args: &[&str]| -> Result<()> {
            let output = Command::new("git")
                .arg("-C")
                .arg(checkout)
                .arg("--work-tree")
                .arg(&destination)
                .args([
                    "-c",
                    "core.bare=false",
                    "-c",
                    "core.symlinks=false",
                    "-c",
                    "core.autocrlf=false",
                ])
                .args(args)
                .env("GIT_INDEX_FILE", index.path().join(directory))
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_NO_LAZY_FETCH", "1")
                .output()?;
            ensure!(
                output.status.success(),
                "Could not materialize pinned Git tree with {}",
                args[0]
            );
            Ok(())
        };
        run(&["read-tree", reference])?;
        run(&["checkout-index", "--all"])?;
    }
    let comparison = format!("{}...{}", snapshot.base, snapshot.head);
    let diff = git(
        checkout,
        &["diff", "--no-ext-diff", "--no-textconv", &comparison, "--"],
    )?;
    fs::write(pr.join("diff.patch"), diff)?;
    fs::write(pr.join("pr.json"), serde_json::to_vec(snapshot)?)?;
    Ok(())
}

fn snapshot_tools(
    mount: Mount,
    snapshot: &Snapshot,
    settings: &config::ReviewConfig,
) -> Result<QuickJsTools> {
    ensure!(
        &metadata(&mount.root)? == snapshot,
        "Mounted snapshot does not match the review request"
    );
    for directory in ["repo", "base"] {
        regular_path(&mount.root, directory, true).with_context(|| {
            format!("Missing or invalid mounted evidence directory {directory}")
        })?;
    }
    regular_path(&mount.root, "pr/diff.patch", false)?;
    let mut tools = QuickJsTools::with_limits(settings.script);
    let mount = Arc::new(mount);
    let read_mount = mount.clone();
    tools.register_rust(
        "fs_read_text",
        "Read text from a file in the review snapshot, up to 200 lines per page.",
        json!({"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer","minimum":1},"max_lines":{"type":"integer","minimum":1,"maximum":200}},"required":["path"],"additionalProperties":false}),
        move |args| {
            let path = path(&args)?;
            let limit = if path == "pr/diff.patch" { 128 * 1024 * 1024 } else { 2_000_000 };
            lines(&read(&read_mount.root, path, limit)?, &args)
        },
    )?;
    tools.register_rust(
        "fs_list_dir",
        "List files and directories in the review snapshot, up to 200 entries per page.",
        json!({"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer","minimum":0}},"required":["path"],"additionalProperties":false}),
        move |args| {
            let path = path(&args)?;
            let offset = args.get("offset").map_or(Some(0), Value::as_u64)
                .context("Invalid directory offset")?;
            let offset = usize::try_from(offset)?;
            let mut entries = Vec::new();
            for entry in fs::read_dir(regular_path(&mount.root, path, true)?)? {
                let entry = entry?;
                let Some(name) = entry.file_name().into_string().ok() else { continue };
                let kind = entry.file_type()?;
                if name.eq_ignore_ascii_case(".git") || !(kind.is_file() || kind.is_dir()) {
                    continue;
                }
                if path == "pr" && !matches!(name.as_str(), "pr.json" | "diff.patch") {
                    continue;
                }
                entries.push((name, if kind.is_dir() { "directory" } else { "file" }));
            }
            entries.sort_unstable();
            let next_offset = (entries.len().saturating_sub(offset) > 200).then(|| offset + 200);
            let entries: Vec<_> = entries.into_iter().skip(offset).take(200)
                .map(|(name, kind)| json!({"name": name, "type": kind})).collect();
            Ok(json!({"entries": entries, "next_offset": next_offset}))
        },
    )?;
    Ok(tools)
}

pub(crate) fn review(
    snapshot: &Snapshot,
    mount: impl FnOnce() -> Result<Mount>,
    access_token: impl Fn() -> Result<String>,
) -> Result<Value> {
    snapshot.validate()?;
    #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
    let runtime = Runtime::new()?;
    #[cfg(all(target_os = "wasi", target_env = "p2"))]
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()?;
    let settings = config::ReviewConfig::from_env()?;
    let input = "Review the pull request in the mounted snapshot.";
    let request_digest = hex::encode(Sha256::digest(serde_json::to_vec(snapshot)?));
    let identity = format!(
        "{}#{}:{}:{}:mounted-fs-v2:{}:{request_digest}",
        snapshot.nwo, snapshot.pr_number, snapshot.head, snapshot.base, settings.model
    );
    let result = review::run_agent(
        &runtime,
        &settings,
        &model::client()?,
        || snapshot_tools(mount()?, snapshot, &settings),
        &identity,
        input,
        access_token,
    )?;
    review::benchmark_findings(
        &snapshot.nwo,
        snapshot.pr_number,
        &snapshot.base,
        &snapshot.head,
        &result,
    )
}

#[cfg(not(target_os = "wasi"))]
pub(crate) fn fetch(snapshot: &Snapshot, token: &str) -> Result<tempfile::TempDir> {
    use base64::{Engine, engine::general_purpose::STANDARD};
    ensure!(review::allowed_repo(&snapshot.nwo), "Invalid repository");
    ensure!(
        review::valid_sha(&snapshot.base) && review::valid_sha(&snapshot.head),
        "Invalid PR commit SHA"
    );
    let dir = tempfile::tempdir()?;
    git(dir.path(), &["init", "--bare", "--template="])?;
    let url = format!("https://github.com/{}.git", snapshot.nwo);
    let authorization = STANDARD.encode(format!("x-access-token:{token}"));
    let status = Command::new("git")
        .arg("-C")
        .arg(dir.path())
        .args([
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            &url,
            &snapshot.base,
            &snapshot.head,
        ])
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "http.https://github.com/.extraheader")
        .env(
            "GIT_CONFIG_VALUE_0",
            format!("Authorization: Basic {authorization}"),
        )
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()?;
    ensure!(
        status.status.success(),
        "Git could not fetch the pinned PR commits"
    );
    git(
        dir.path(),
        &["update-ref", "--no-deref", "HEAD", &snapshot.head],
    )?;
    Ok(dir)
}

#[cfg(all(test, not(target_os = "wasi")))]
mod tests {
    use super::*;

    #[test]
    fn pinned_git_objects_seed_reviewbench_mount_and_quickjs_reads_only_evidence() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let repo = tmp.path().join("source");
        fs::create_dir(&repo)?;
        git(&repo, &["init", "--template="])?;
        let commit = |message| -> Result<String> {
            git(&repo, &["add", "--all"])?;
            git(
                &repo,
                &[
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    "commit",
                    "-m",
                    message,
                ],
            )?;
            Ok(String::from_utf8(git(&repo, &["rev-parse", "HEAD"])?)?
                .trim()
                .to_owned())
        };
        fs::write(repo.join("example.txt"), "old text\n")?;
        let base = commit("base")?;
        fs::write(repo.join("example.txt"), "new text\nsecond line\n")?;
        let head = commit("head")?;
        fs::write(repo.join("example.txt"), "unstaged text\n")?;
        let bare = tmp.path().join("objects");
        git(tmp.path(), &["clone", "--bare", "source", "objects"])?;
        let mut snapshot = Snapshot {
            repo: Some("acme_project".into()),
            nwo: "acme/project".into(),
            pr_number: 7,
            base,
            head,
            title: Some("Example".into()),
            body: None,
        };
        let mount = temporary_mount(&bare, &snapshot)?;
        assert_eq!(
            fs::read_to_string(mount.root.join("repo/example.txt"))?,
            "new text\nsecond line\n"
        );
        assert_eq!(
            fs::read_to_string(mount.root.join("base/example.txt"))?,
            "old text\n"
        );
        assert!(fs::read_to_string(mount.root.join("pr/diff.patch"))?.contains("+new text"));
        assert_eq!(metadata(&mount.root)?, snapshot);
        let settings = config::ReviewConfig {
            model: "fixture".into(),
            github_timeout: std::time::Duration::from_secs(5),
            script: Default::default(),
        };
        snapshot.pr_number += 1;
        assert!(snapshot_tools(Mount::supplied(mount.root.clone()), &snapshot, &settings).is_err());
        snapshot.pr_number -= 1;

        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(repo.join("example.txt"), mount.root.join("repo/escape"))?;
            assert!(read(&mount.root, "repo/escape", 100).is_err());
        }
        let tools = snapshot_tools(mount, &snapshot, &settings)?;
        let script = r#"
            const [head, base, patch, directory] = await Promise.all([
                fs.readText("/work/repo/example.txt", {maxLines: 1}),
                fs.readText("/work/base/example.txt"),
                fs.readText("/work/pr/diff.patch"),
                fs.listDir("/work/repo"),
            ]);
            if (head.next_line !== 2) throw new Error("missing next page");
            const next = await fs.readText("/work/repo/example.txt", {startLine: head.next_line});
            if (next.text.trim() !== "second line" || next.next_line !== null)
                throw new Error("wrong next page");
            if (!patch.text.includes("+new text")) throw new Error("wrong patch");
            if (base.text.trim() !== "old text") throw new Error("wrong base");
            if (directory.entries.length !== 1 || directory.entries[0].name !== "example.txt")
                throw new Error("unexpected directory listing");
            console.log(head.text.trim());
        "#;
        let args = json!({"code": script}).to_string();
        assert_eq!(
            Runtime::new()?.block_on(tools.call("run_js", &args, false))?,
            json!("new text\n")
        );
        for path in [
            "/work/repo/../pr/pr.json",
            "/work/repo/escape",
            "/work/out/findings.json",
        ] {
            let code = format!("await fs.readText({path:?})");
            let args = json!({"code": code}).to_string();
            assert!(
                Runtime::new()?
                    .block_on(tools.call("run_js", &args, false))
                    .is_err()
            );
        }
        Ok(())
    }
}
