#!/usr/bin/env python3
"""Reproducible compatibility overlay; leaves the native source/dependencies intact.
Prerequisites: cargo-wasix 0.1.34+, official WASI SDK clang, Python3, network access.
Set CC_wasm32_wasmer_wasi to the WASI SDK clang binary when the host clang lacks WASM.
"""
import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--features', default='')
parser.add_argument('--update-lock', action='store_true', help='Resolve and save an updated WASIX lockfile')
args = parser.parse_args()

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.wasix-build'
PROJECT = BUILD / 'project'
SDK = BUILD / 'rmcp'
VERSION = '3.10.0'
CHECKSUM = 'b5949e504d5ab9944a168259afc0c39ac0230627b6ec5367ace194c59bcbe163'
PROJECT.mkdir(parents=True, exist_ok=True)
# Copy only explicitly intended source inputs, never .env, auth stores, .git or targets.
for name in ['Cargo.toml', 'README.md']:
    shutil.copy2(ROOT / name, PROJECT / name)
for name in ['src', 'tests', 'migrations']:
    if (PROJECT / name).exists():
        shutil.rmtree(PROJECT / name)
    shutil.copytree(ROOT / name, PROJECT / name)


def unpack_crate(name, version, checksum, destination):
    root = f'{name}-{version}'
    url = f'https://static.crates.io/crates/{name}/{root}.crate'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != checksum:
        raise SystemExit(f'{name} crate checksum mismatch')
    if destination.exists():
        shutil.rmtree(destination)
    source = BUILD / root
    if source.exists():
        shutil.rmtree(source)
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for member in tar.getmembers():
            if (not (member.isfile() or member.isdir())
                    or '..' in Path(member.name).parts
                    or not member.name.startswith(f'{root}/')):
                raise SystemExit(f'Unexpected {name} crate archive entry')
        tar.extractall(BUILD)
    source.rename(destination)


unpack_crate('rmcp', VERSION, CHECKSUM, SDK)
quickjs = BUILD / 'rquickjs-sys'
unpack_crate('rquickjs-sys', '0.14.0',
             'cee271d0eeba64f0915b846cb7ae02e16faf3dfdffdca91731101d9d30fe3423',
             quickjs)
bindings = quickjs / 'src' / 'bindings'
shutil.copy2(bindings / 'wasm32-wasip1.rs', bindings / 'wasm32-wasmer-wasi.rs')
subprocess.run(['patch', '-p1', '-i', str(ROOT / 'wasix/quickjs-probe/wasix-clock.patch')],
               cwd=quickjs, check=True)
build_script = quickjs / 'build.rs'
source = build_script.read_text()
anchor = '    builder.compile("libquickjs.a");'
assert source.count(anchor) == 1
source = source.replace(anchor, '''    if env::var("TARGET").as_deref() == Ok("wasm32-wasmer-wasi") {
        builder.flag("--target=wasm32-wasip1").define("QUICKJS_WASIX_PROBE", None);
    }
''' + anchor)
build_script.write_text(source)

manifest = (SDK / 'Cargo.toml').read_text()
assert 'version = "0.13.2"' in manifest
manifest = manifest.replace('version = "0.13.2"', 'version = "0.12"')
manifest = manifest.replace('reqwest?/rustls', 'reqwest?/rustls-tls')
(SDK / 'Cargo.toml').write_text(manifest)
# Octocrab uses Hyper here. Its optional reqwest dependency still needs to
# resolve against the version available from the overlay registry.
octocrab = BUILD / 'octocrab'
unpack_crate('octocrab', '0.54.9',
             '319b72643e8fa16dc3278f66439534513b2b65d47ec9cf2b26e7316c0c8ef24a',
             octocrab)
manifest = (octocrab / 'Cargo.toml').read_text()
anchor = '[dependencies.reqwest]\nversion = "0.13"'
assert manifest.count(anchor) == 1
manifest = manifest.replace(anchor, '[dependencies.reqwest]\nversion = "0.12"')
manifest = manifest.replace('reqwest?/rustls"', 'reqwest?/rustls-tls"')
(octocrab / 'Cargo.toml').write_text(manifest)

manifest = (PROJECT / 'Cargo.toml').read_text()
# The deployed Wasmer listener still uses its WASIX dependency overlay.
# Omit the separate Preview 2 command's dependencies from its lockfile.
preview2 = """[target.'cfg(all(target_os = "wasi", target_env = "p2"))'.dependencies]
wasi = "0.14"

"""
assert manifest.count(preview2) == 1
manifest = manifest.replace(preview2, '')
native = """[target.'cfg(not(target_os = "wasi"))'.dependencies]
tempfile = "3"

"""
assert manifest.count(native) == 1
manifest = manifest.replace(native, '')
manifest = manifest.replace('sqlite-backend = ["dep:rusqlite"]', 'sqlite-backend = []')
manifest = manifest.replace('rusqlite = { version = "0.37", features = ["bundled"], optional = true }\n', '')
old = 'reqwest = { version = "0.13", default-features = false, features = ["blocking", "json", "rustls", "form"] }'
new = 'reqwest = { version = "0.12", default-features = false, features = ["blocking", "json", "rustls-tls", "stream"] }'
assert old in manifest
manifest = manifest.replace(old, new)
# WASIX has no native CA store. Use the bundled public roots for GitHub TLS.
old_roots = '"default-client", "rustls", "rustls-ring"'
assert manifest.count(old_roots) == 1
manifest = manifest.replace(old_roots, old_roots + ', "rustls-webpki-tokio"')
manifest += '\n[patch.crates-io]\nrmcp = { path = "../rmcp" }\nrquickjs-sys = { path = "../rquickjs-sys" }\noctocrab = { path = "../octocrab" }\n'
(PROJECT / 'Cargo.toml').write_text(manifest)
lock = ROOT / 'wasix' / 'Cargo.lock'
if lock.exists():
    shutil.copy2(lock, PROJECT / 'Cargo.lock')
command = ['cargo', 'wasix', 'build', '--release', '--bin', 'wrev']
command += ['--features', ','.join(filter(None, ['quickjs-tools', args.features]))]
if not args.update_lock:
    command += ['--locked']
subprocess.run(command, cwd=PROJECT, check=True,
               env=dict(os.environ, RQUICKJS_SYS_NO_WASI_SDK='1'))
(ROOT / 'dist').mkdir(exist_ok=True)
shutil.copy2(PROJECT / 'target/wasm32-wasmer-wasi/release/wrev.wasm', ROOT / 'dist/wrev.wasm')
if args.update_lock:
    shutil.copy2(PROJECT / 'Cargo.lock', lock)
print('Built dist/wrev.wasm; no credentials or environment files packaged.')
