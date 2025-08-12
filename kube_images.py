from __future__ import annotations

"""
List Kubernetes container images per namespace, with digest-aware de-duplication.
- Scans one or many kube-contexts (optionally in parallel).
- Prefers digest-qualified image references for stable identity.
- Falls back to spec.containers when status isn't populated (e.g., Pending pods).
- Skips malformed/empty image refs (e.g., ":", ":v1.2.3", "@sha256:...").

Library ergonomics:
- Use `scan_images(...)` for a one-call convenience wrapper that returns a `ScanResult`
  (with `contexts` and `errors`) and can optionally write JSON.
- Or use `KubernetesImageScanner` directly for finer control.

CLI output shape:
{
  "contexts": { "<context>": { "<namespace>": [ {ref,name,digest}, ... ] } },
  "errors":   { "<context>": "<error message>", ... }
}
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
from kubernetes import client, config
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

# Silence warnings about self-signed clusters we don't control
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# --------------------------- Models ---------------------------

@dataclass(frozen=True)
class ContainerImage:
    """
    Canonical representation of an image found in the cluster.

    Attributes:
      ref:    Normalized image reference. If a digest is known, this includes '@<algo>:<hex>'.
      name:   Short image name (repository tail without tag/digest), e.g., 'nginx'.
      digest: Content digest like 'sha256:...' when known, else None.
    """
    ref: str
    name: str
    digest: Optional[str]

    def to_dict(self) -> Dict[str, Optional[str]]:
        return asdict(self)


# Structured scan result for library users
@dataclass(frozen=True)
class ScanResult:
    contexts: Dict[str, Dict[str, List[Dict[str, Optional[str]]]]]
    errors: Dict[str, str]


# --------------------------- Parsing & Normalization ---------------------------

class ImageReferenceParser:
    """
    Utilities for extracting and normalizing container image references.
    Kept as a cohesive unit for readability and easy testing.
    """

    # Generic "<algo>:<hex>" digest (future-proof beyond sha256)
    _DIGEST_RE = re.compile(r"([A-Za-z0-9_+.\-]+):([A-Fa-f0-9]{32,128})")
    # Detect a digest embedded in a reference via "@<algo>:<hex>"
    _AT_DIGEST_RE = re.compile(r"@([A-Za-z0-9_+.\-]+):([A-Fa-f0-9]{32,128})")
    _SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

    @staticmethod
    def strip_scheme(s: str) -> str:
        """Remove leading URI schemes like docker://, containerd://, cri-o://, etc."""
        if not s:
            return s
        return ImageReferenceParser._SCHEME_RE.sub("", s)

    @staticmethod
    def is_valid_image_ref(image: Optional[str]) -> bool:
        """
        Basic sanity check: require a non-empty repo/name before any ':tag' or '@digest'.
        Reject "", ":", ":v1.2.3", "@sha256:...", etc.
        """
        s = ImageReferenceParser.strip_scheme((image or "").strip())
        if not s:
            return False
        if s.startswith(":") or s.startswith("@"):
            return False
        # Repo/name part before any ':tag' or '@digest'
        name_part = re.split(r"[@:]", s, 1)[0]
        # Must contain at least one alphanumeric (handles things like "library/" edge cases)
        if not re.search(r"[A-Za-z0-9]", name_part):
            return False
        return True

    @staticmethod
    def extract_digest(*candidates: Optional[str]) -> Optional[str]:
        """
        Return '<algo>:<hex>' if found in any candidate string; otherwise None.
        Examples: 'sha256:...', 'sha512:...'
        """
        for candidate in candidates:
            if not candidate:
                continue
            m = ImageReferenceParser._DIGEST_RE.search(candidate)
            if m:
                algo, hexd = m.group(1), m.group(2)
                return f"{algo}:{hexd}"
        return None

    @staticmethod
    def short_name(image_ref: str) -> str:
        """
        Derive a short, human-friendly image name from a normalized reference.
        Examples:
          ghcr.io/org/app:1.2.3          -> app
          registry.local:5000/ns/app@... -> app
        """
        if not image_ref:
            return "unknown"
        no_scheme = ImageReferenceParser.strip_scheme(image_ref)
        tail = no_scheme.rsplit("/", 1)[-1]
        tail = tail.split("@", 1)[0]      # drop digest suffix if present
        if ":" in tail:
            tail = tail.split(":", 1)[0]  # drop tag if present
        return tail or "unknown"

    @staticmethod
    def compose_reference(image: str, digest: Optional[str]) -> str:
        """
        Prefer a digest-qualified reference for stable identity. If 'image' already includes
        '@<algo>:<hex>', keep it. Otherwise, append '@<digest>' when available.
        """
        normalized = ImageReferenceParser.strip_scheme(image or "")
        if ImageReferenceParser._AT_DIGEST_RE.search(normalized):
            return normalized
        if digest:
            return f"{normalized}@{digest}"
        return normalized

    @staticmethod
    def uniqueness_key(ref: str, digest: Optional[str]) -> str:
        """
        Per-namespace uniqueness key: prefer digest (content identity),
        otherwise use the reference string (case-insensitive).
        """
        return (digest or ref).lower()

    @staticmethod
    def from_statuses(statuses: Optional[List[dict]]) -> List[ContainerImage]:
        """
        Build images from containerStatuses-like objects, where imageID typically includes the digest.
        """
        results: List[ContainerImage] = []
        if not isinstance(statuses, list):
            return results

        for status in statuses:
            image = ImageReferenceParser.strip_scheme(status.get("image") or "")
            image_id = ImageReferenceParser.strip_scheme(status.get("imageID") or "")

            # Skip junk like ":" or ":v1.2.3"
            if not ImageReferenceParser.is_valid_image_ref(image):
                log.debug("Skipping invalid image ref from status: %r", image)
                continue

            digest = ImageReferenceParser.extract_digest(image_id, image)
            ref = ImageReferenceParser.compose_reference(image, digest)
            name = ImageReferenceParser.short_name(ref or image)
            results.append(ContainerImage(ref=ref, name=name, digest=digest))
        return results

    @staticmethod
    def from_container_specs(containers: Optional[List[dict]]) -> List[ContainerImage]:
        """
        Build images from spec.containers[].image when status is unavailable (e.g., Pending pods).
        """
        results: List[ContainerImage] = []
        if not isinstance(containers, list):
            return results

        for container in containers:
            image = ImageReferenceParser.strip_scheme(container.get("image") or "")

            # Skip junk like ":" or ":v1.2.3"
            if not ImageReferenceParser.is_valid_image_ref(image):
                log.debug("Skipping invalid image ref from spec: %r", image)
                continue

            digest = ImageReferenceParser.extract_digest(image)
            ref = ImageReferenceParser.compose_reference(image, digest)
            name = ImageReferenceParser.short_name(ref or image)
            results.append(ContainerImage(ref=ref, name=name, digest=digest))
        return results


# --------------------------- Kubernetes Wiring ---------------------------

class KubernetesClientFactory:
    """Creates Kubernetes API clients and lists contexts. Isolated for testability."""

    def list_context_names(self, kubeconfig_path: Optional[str]) -> List[str]:
        log.info("Loading kubeconfig from: %s", kubeconfig_path or "<default>")
        contexts, _ = config.list_kube_config_contexts(config_file=kubeconfig_path)
        names = [c["name"] for c in contexts]
        log.info("Found %d context(s): %s", len(names), names)
        return names

    def new_core_v1(
        self,
        context_name: str,
        kubeconfig_path: Optional[str],
    ) -> Tuple[ApiClient, client.CoreV1Api]:
        log.info("Creating API client for context: %s (kubeconfig=%s)", context_name, kubeconfig_path or "<default>")
        api_client = config.new_client_from_config(context=context_name, config_file=kubeconfig_path)
        return api_client, client.CoreV1Api(api_client=api_client)


# --------------------------- Scanning ---------------------------

class KubernetesImageScanner:
    """
    Scans one kube-context for images per namespace (digest-aware dedupe),
    and coordinates parallel scans across multiple contexts.
    """

    CONTAINER_STATUS_KEYS: Tuple[str, ...] = (
        "containerStatuses",
        "initContainerStatuses",
        "ephemeralContainerStatuses",
    )

    def __init__(self, kubeconfig_path: Optional[str], client_factory: Optional[KubernetesClientFactory] = None) -> None:
        self.kubeconfig_path = kubeconfig_path
        self.client_factory = client_factory or KubernetesClientFactory()

    # ---- Discovery entry points ------------------------------------------------

    def list_all_context_names(self) -> List[str]:
        return self.client_factory.list_context_names(self.kubeconfig_path)

    def scan_multiple_contexts(
        self,
        context_names: Iterable[str],
        max_workers: Optional[int] = None,
        page_limit: Optional[int] = None,
        request_timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Scan many contexts in parallel.

        Returns:
          {
            "<context>": { "<namespace>": [ {ref,name,digest}, ... ] },
            "__errors__": { "<context>": "<error message>", ... }  # present only if failures occur
          }
        """
        names = list(context_names)
        if not names:
            return {}

        # Higher default worker cap for I/O-bound API calls
        worker_count = max_workers if max_workers is not None else min(32, len(names))
        log.info("Scanning %d context(s) with up to %d worker(s)…", len(names), worker_count)

        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self.scan_single_context,
                    name,
                    page_limit,
                    request_timeout_seconds,
                ): name
                for name in names
            }

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except ApiException as exc:
                    msg = f"Kubernetes API error: {exc}"
                    errors[name] = msg
                    log.error("[context=%s] %s", name, msg)
                except Exception as exc:
                    msg = f"Unexpected error: {exc.__class__.__name__}: {exc}"
                    errors[name] = msg
                    log.exception("[context=%s] %s", name, msg)

        if errors:
            results["__errors__"] = errors

        return results

    def scan_single_context(
        self,
        context_name: str,
        page_limit: Optional[int] = None,
        request_timeout_seconds: Optional[int] = None,
    ) -> Dict[str, List[Dict[str, Optional[str]]]]:
        """
        Scan one context and return images per namespace.
        Returns:
          { "<namespace>": [ {ref,name,digest}, ... ] }
        """
        api_client, core_v1 = self.client_factory.new_core_v1(context_name, self.kubeconfig_path)

        log.info("[context=%s] Listing pods across all namespaces…", context_name)
        pod_list_dict = self._list_pods_across_all_namespaces(
            core_v1,
            api_client,
            page_limit,
            request_timeout_seconds,
        )
        pods = pod_list_dict.get("items", [])
        log.info("[context=%s] Retrieved %d pod(s)", context_name, len(pods))

        # ns -> (uniqueness_key -> ContainerImage)
        images_by_namespace: Dict[str, Dict[str, ContainerImage]] = {}

        for pod in pods:
            namespace = (pod.get("metadata", {}) or {}).get("namespace") or "default"
            ns_bucket = images_by_namespace.setdefault(namespace, {})

            status_section = (pod.get("status", {}) or {})
            collected: List[ContainerImage] = []

            for key in self.CONTAINER_STATUS_KEYS:
                collected.extend(ImageReferenceParser.from_statuses(status_section.get(key)))

            if not collected:
                spec_containers = (pod.get("spec", {}) or {}).get("containers", [])
                collected.extend(ImageReferenceParser.from_container_specs(spec_containers))

            for image in collected:
                ukey = ImageReferenceParser.uniqueness_key(image.ref, image.digest)
                # first one wins; all carry same identity
                ns_bucket.setdefault(ukey, image)

        # Stable, readable output: sort by (name, ref) and convert to dicts
        result: Dict[str, List[Dict[str, Optional[str]]]] = {}
        for namespace, image_map in images_by_namespace.items():
            sorted_images = sorted(image_map.values(), key=lambda x: (x.name, x.ref))
            result[namespace] = [img.to_dict() for img in sorted_images]
            log.info("[context=%s] namespace=%s -> %d unique image(s)", context_name, namespace, len(sorted_images))

        return result

    # ---- Kubernetes pagination -------------------------------------------------

    @staticmethod
    def _list_pods_across_all_namespaces(
        core_v1: client.CoreV1Api,
        api_client: ApiClient,
        page_limit: Optional[int],
        request_timeout_seconds: Optional[int],
    ) -> Dict:
        """
        List pods across all namespaces with pagination, returning a plain dict
        (via ApiClient.sanitize_for_serialization) compatible with our processing.
        """
        items: List[dict] = []
        continue_token: Optional[str] = None

        while True:
            response = core_v1.list_pod_for_all_namespaces(
                limit=page_limit,
                _continue=continue_token,
                watch=False,
                _request_timeout=request_timeout_seconds,
            )
            data = api_client.sanitize_for_serialization(response)
            items.extend(data.get("items", []))
            continue_token = (data.get("metadata") or {}).get("continue")

            if not continue_token:
                break

            log.debug("Continuing pod list pagination with token length=%d", len(continue_token))

        return {"items": items}


# --------------------------- I/O helpers ---------------------------

class KubeconfigLocator:
    """Resolves which kubeconfig path to use."""

    @staticmethod
    def resolve(explicit_path: Optional[str]) -> Optional[str]:
        if explicit_path:
            return explicit_path
        env_path = os.getenv("KUBECONFIG")
        if env_path:
            return env_path
        default_path = os.path.expanduser("~/.kube/config")
        return default_path if os.path.exists(default_path) else None


class JsonWriter:
    """Minimal JSON writer for CLI/library output."""

    @staticmethod
    def write(data, destination: Optional[str], pretty: bool) -> None:
        text = json.dumps(data, indent=2 if pretty else None, sort_keys=False)
        if not destination or destination == "-":
            sys.stdout.write(text + "\n")
            return
        with open(destination, "w", encoding="utf-8") as fh:
            fh.write(text)


# --------------------------- Library convenience wrapper ---------------------------

def scan_images(
    kubeconfig_path: Optional[str] = None,
    contexts: Optional[List[str]] = None,
    all_contexts: bool = False,
    max_workers: Optional[int] = None,
    page_limit: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    output_path: Optional[str] = None,   # "-" writes to stdout
    pretty: bool = False,
) -> ScanResult:
    """
    Convenience wrapper for library users: discover contexts (if needed), scan,
    optionally write JSON, and return structured results.

    Precedence:
      - If `all_contexts` is True, ignores `contexts` and scans all.
      - Else if `contexts` is provided, scans those.
      - Else scans all contexts by default.
    """
    scanner = KubernetesImageScanner(kubeconfig_path=kubeconfig_path)

    if all_contexts or contexts is None:
        chosen_contexts = scanner.list_all_context_names()
    else:
        chosen_contexts = contexts

    raw = scanner.scan_multiple_contexts(
        chosen_contexts,
        max_workers=max_workers,
        page_limit=page_limit,
        request_timeout_seconds=timeout_seconds,
    )

    errors = {}
    if isinstance(raw, dict) and "__errors__" in raw:
        errors = raw.pop("__errors__", {}) or {}

    contexts_map: Dict[str, Dict[str, List[Dict[str, Optional[str]]]]] = {
        k: v for k, v in raw.items() if isinstance(v, dict)
    }

    if output_path is not None:
        JsonWriter.write({"contexts": contexts_map, "errors": errors}, output_path, pretty)

    return ScanResult(contexts=contexts_map, errors=errors)


# --------------------------- CLI ---------------------------

class CommandLineApp:
    """Thin CLI wrapper that wires arguments to the scanner."""

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(
            description="List Kubernetes container images per namespace, with digest-aware de-duplication."
        )
        self._define_arguments(self._parser)

    @staticmethod
    def _define_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--kubeconfig",
            help="Path to kubeconfig. Defaults to $KUBECONFIG or ~/.kube/config if present.",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--context",
            action="append",
            help="Specific context(s) to query. Can be repeated.",
        )
        group.add_argument(
            "--all-contexts",
            action="store_true",
            help="Query all contexts in the kubeconfig.",
        )
        parser.add_argument(
            "--output", "-o",
            default="-",
            help="Output file path (JSON). Use '-' for stdout (default).",
        )
        parser.add_argument(
            "--pretty",
            action="store_true",
            help="Pretty-print JSON output.",
        )
        parser.add_argument(
            "--max-workers",
            type=int,
            help="Max parallel contexts (default: min(32, number of contexts))).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Kubernetes list page size per request (default: unlimited).",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Per-API-call timeout in seconds (default: none).",
        )
        parser.add_argument(
            "--log-level",
            default="INFO",
            choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
            help="Logging verbosity.",
        )

    def run(self, argv: Optional[List[str]] = None) -> int:
        args = self._parser.parse_args(argv)

        logging.basicConfig(
            level=getattr(logging, args.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
        )

        kubeconfig_path = KubeconfigLocator.resolve(args.kubeconfig)

        try:
            res = scan_images(
                kubeconfig_path=kubeconfig_path,
                contexts=args.context,
                all_contexts=args.all_contexts or not args.context,  # default to "all" if neither supplied
                max_workers=args.max_workers,
                page_limit=args.limit,
                timeout_seconds=args.timeout,
                output_path=args.output,      # "-" -> stdout
                pretty=args.pretty,
            )
            # Nothing else to do; scan_images already wrote to output_path if provided.
            # Exit code reflects severe top-level errors only; per-context errors are in JSON.
            return 0

        except FileNotFoundError:
            log.error("Kubeconfig not found. Provide --kubeconfig or set $KUBECONFIG.")
            return 2
        except ApiException as exc:
            log.error("Kubernetes API error: %s", exc)
            return 3
        except Exception:
            log.exception("Unhandled error")
            return 4


# --------------------------- Entrypoint ---------------------------

def main(argv: Optional[List[str]] = None) -> int:
    return CommandLineApp().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
