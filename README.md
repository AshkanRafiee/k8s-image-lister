# k8s-image-lister

> **k8s-image-lister** helps you quickly enumerate every container image running in your Kubernetes clusters.
>
> This repo now contains **two complementary tools**:
>
> 1. **`kube-images.sh`** — a single‑file Bash utility for a fast, colorful table view or JSON export (works anywhere `kubectl` + `jq` are available).
>
> 2. **`kube_images.py`** — a Python CLI/library that scans one or many kube‑contexts (optionally in parallel), prefers digest‑qualified references for stable identity, and provides a consistent JSON output you can consume in scripts or apps.

---

## ✨ Highlights

| Area              | Bash: `kube-images.sh`                                                              | Python: `kube_images.py`                                                      |
| ----------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| **Scope control** | One context/namespace, *all* contexts, or interactive prompts                       | One or many contexts; scan all contexts by default; optional parallelism      |
| **Identity**      | `--mode id` (digest from `status.containerStatuses`) or `--mode image` (spec image) | Digest‑aware de‑duplication; always prefers `@<algo>:<hex>` when known        |
| **Output**        | Colorized table **or** JSON (`flat` or `pod` hierarchies)                           | JSON only; stable shape with `{ref,name,digest}` entries + per‑context errors |
| **Deps**          | `bash`, `kubectl`, `jq`                                                             | Python 3.8+, deps in `requirements.txt`                                       |
| **Good for**      | Quick audits in a terminal; copy/paste lists                                        | Programmatic use, CI, large clusters, multi‑context parallel scanning         |

---

## 📦 Installation

### Clone

```bash
# Clone the repo
git clone https://github.com/AshkanRafiee/k8s-image-lister.git
cd k8s-image-lister
```

### Bash tool (`kube-images.sh`)

```bash
# Make executable
chmod +x kube-images.sh

# (Optional) put on your $PATH
sudo mv kube-images.sh /usr/local/bin/k8s-image-lister
```

**Requirements:** Bash 5+, `kubectl` (≥ 1.19 recommended), `jq` 1.5+

### Python tool (`kube_images.py`)

```bash
# (Optional) create/activate a virtualenv first
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Make the script executable if you like
chmod +x kube_images.py
```

---

## 🛠️ Usage

### Bash — `kube-images.sh`

```text
k8s-image-lister [options]

  -c, --context   <ctx|all|ask>   Context to inspect         (default ask)
  -n, --namespace <ns|all|ask>    Namespace filter           (default ask)
  -m, --mode      <image|id>      Show tag or digest         (default id)
  -f, --format    <table|json>    Output format              (default table)
  -s, --json-style <flat|pod>     JSON hierarchy when -f json (default pod)
  -o, --output    <file>          JSON file path (with -f json)
  --kubeconfig    <path>          Use a non‑default kubeconfig
  -h, --help                      Show help & exit
```

**Common examples**

| Goal                                                          | Command                                                    |
| ------------------------------------------------------------- | ---------------------------------------------------------- |
| Interactive prompts for context + namespace; colourised table | `k8s-image-lister`                                         |
| Scan **all** contexts, ask for namespaces per context         | `k8s-image-lister -c all -n ask`                           |
| Single context & namespace, digest table                      | `k8s-image-lister -c prod -n kube-system`                  |
| Export *flat* JSON with image tags                            | `k8s-image-lister -m image -f json -s flat -o images.json` |

#### JSON structures (Bash)

**`flat`**\*\* style\*\*

```jsonc
{
  "prod": {
    "kube-system": [
      "registry/k8s.gcr.io/kube-apiserver@sha256:…",
      "registry/k8s.gcr.io/coredns@sha256:…"
    ]
  }
}
```

**`pod`**\*\* style (default)\*\*

```jsonc
{
  "prod": {
    "default": {
      "my-app-6d9cc7bf7": [
        "registry.example.com/app:v1.0.0",
        "registry.example.com/helper:v2.1.3"
      ]
    }
  }
}
```

---

### Python — `kube_images.py` (CLI)

The Python scanner always emits JSON. It favors digest‑qualified refs, de‑duplicates by digest per namespace, and can scan contexts in parallel.

```text
usage: kube_images.py [--kubeconfig PATH] [--context CTX] [--all-contexts]
                            [--output PATH] [--pretty] [--max-workers N]
                            [--limit N] [--timeout SECONDS]
                            [--log-level {CRITICAL,ERROR,WARNING,INFO,DEBUG}]
```

**Options**

* `--kubeconfig PATH` — path to kubeconfig (defaults to `$KUBECONFIG` or `~/.kube/config` when present)
* `--context CTX` (repeatable) — one or more contexts to scan
* `--all-contexts` — scan all contexts (default if no `--context` is given)
* `-o, --output PATH` — write JSON to file; `-` writes to stdout (default)
* `--pretty` — pretty‑print JSON
* `--max-workers N` — number of parallel context workers (default: `min(32, number_of_contexts)`)
* `--limit N` — Kubernetes list page size per request (pagination)
* `--timeout SECONDS` — per‑API‑call timeout
* `--log-level …` — logging verbosity (default `INFO`)

**Examples**

```bash
# Scan every context and pretty‑print to stdout
./kube_images.py --all-contexts --pretty

# Scan two contexts only, limit page size, timeout after 30s per API call
./kube_images.py --context prod --context staging --limit 200 --timeout 30 -o images.json --pretty

# Pipe to jq: list all digest‑qualified refs
./kube_images.py --all-contexts | jq -r '.contexts[][][] | select(.digest!=null) | .ref'
```

**Output shape (Python)**

```jsonc
{
  "contexts": {
    "prod": {
      "default": [
        { "ref": "ghcr.io/org/app@sha256:…", "name": "app", "digest": "sha256:…" },
        { "ref": "docker.io/library/redis:7", "name": "redis", "digest": null }
      ]
    }
  },
  "errors": {
    "staging": "Kubernetes API error: …" // present only if some contexts failed
  }
}
```

> De‑duplication key is `digest` when known (content identity), otherwise the reference string (case‑insensitive). The scanner also falls back to `spec.containers[].image` when status isn’t populated (e.g., `Pending` pods).

---

### Python — use as a library

```python
from kube_images import scan_images

result = scan_images(
    kubeconfig_path=None,   # or "/path/to/kubeconfig"
    contexts=["prod", "staging"],
    all_contexts=False,
    max_workers=8,
    page_limit=200,
    timeout_seconds=30,
    output_path=None,       # "-" for stdout, or a file path
    pretty=True,
)

print(result.contexts.keys())  # dict of {context -> {namespace -> [ {ref,name,digest}, ... ]}}
print(result.errors)           # dict of {context -> error_message}
```

---

## 🧭 Which tool should I use?

* **Use Bash** when you want a quick, human‑readable table or one‑off JSON (`flat`/`pod`) without Python deps.
* **Use Python** when you want digest‑aware de‑duplication, parallel scans across many contexts, a consistent schema (`{ref,name,digest}`), or when embedding in CI/scripts.

You can keep both in your toolbox—same goal, different ergonomics.

---

## ⚙️ Notes & Tips

* **Auth & contexts** come from your kubeconfig. The Python tool discovers contexts with the Kubernetes client; Bash shells out to `kubectl`.
* For very large clusters, consider **pagination** (`--limit`) and a **timeout** to avoid long‑running requests.
* The Python tool suppresses `urllib3` insecure warnings (for self‑signed clusters you don’t control). Use proper CA trust in production.
* The Bash script strips `docker-pullable://` prefixes and can read digests from `status.containerStatuses[].imageID` when present.

