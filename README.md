# k8s-image-lister

> **k8s-image-lister** is a single‑file Bash utility that quickly enumerates every container image (tag **or** resolved digest) running in your Kubernetes cluster. It can target one context / namespace, or fan out across **all** contexts with per‑context namespace selection.

---

## ✨ Features

| Capability               | Details                                                                                                                  |                                                 |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| **Flexible scope**       | Pick a single context / namespace, fan out to *all* contexts, or use interactive prompts.                                |                                                 |
| **Digest vs. tag**       | `--mode id` (default) prints the resolved image **digest**; `--mode image` prints the image reference from the Pod spec. |                                                 |
| **Table or JSON output** | Human‑friendly colourised table **or** machine‑readable JSON file.                                                       |                                                 |
| **Two JSON hierarchies** | `flat` = `context → namespace → [images]`                                                                                | `pod` = `context → namespace → pod → [images]`. |
| **Minimal deps**         | Just `bash`, `kubectl`, and `jq`.                                                                                        |                                                 |

---

## 📦 Installation

```bash
# Clone the repo
$ git clone https://github.com/AshkanRafiee/k8s-image-lister.git
$ cd k8s-image-lister

# Make the script executable
$ chmod +x kube-images.sh

# (Optional) move it somewhere on your $PATH
$ sudo mv kube-images.sh /usr/local/bin/k8s-image-lister
```

---

## 🛠️  Usage

```text
k8s-image-lister [options]

  -c, --context   <ctx|all|ask>   Context to inspect         (default ask)
  -n, --namespace <ns|all|ask>    Namespace filter           (default ask)
  -m, --mode      <image|id>      Show tag or digest         (default id)
  -f, --format    <table|json>    Output format              (default table)
  -s, --json-style <flat|pod>     JSON hierarchy when -f json (default pod)
  -o, --output    <file>          JSON file path (with -f json)
  --kubeconfig    <path>          Use a non‑default kubeconfig
  -h, --help                      Show this help & exit
```

### Common examples

| Goal                                                          | Command                                                    |
| ------------------------------------------------------------- | ---------------------------------------------------------- |
| Interactive prompts for context + namespace; colourised table | `k8s-image-lister`                                         |
| Scan **all** contexts, ask for namespaces per context         | `k8s-image-lister -c all -n ask`                           |
| Single context & namespace, digest table                      | `k8s-image-lister -c prod -n kube-system`                  |
| Export *flat* JSON with image tags                            | `k8s-image-lister -m image -f json -s flat -o images.json` |

---

## 🗂️  JSON structures

### `flat` style

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

### `pod` style (default)

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

## ⚙️  Requirements

* Bash 5+
* `kubectl` (≥ 1.19 recommended)
* `jq` 1.5+

---

## 🤝 Contributing

PRs and issues are welcome! Please:

1. Open an issue to discuss new features or bug fixes.
2. Follow existing code style (shellcheck‑clean Bash).
3. Add or update examples in this README where applicable.

---

## 📝 License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.
