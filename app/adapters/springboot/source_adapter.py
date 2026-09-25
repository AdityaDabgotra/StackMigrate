"""
Spring Boot -> IR source adapter.

Scope note: file discovery is intentionally conservative. It only
descends into `src/main/java` (skips test code, build output, `target/`)
and, when `scope_description` mentions a specific module/class name
fragment, filters to files whose path or class name contains that
fragment (case-insensitive). This is what enforces the strangler-fig
"migrate one module" scope from the architecture doc rather than the
LLM silently trying to comprehend an entire monorepo.
"""

from __future__ import annotations

import os
import re

from app.adapters.base import DiscoveredFile, SourceAdapter
from app.graph.state import SemanticUnitKind

_ANNOTATION_TO_KIND: dict[str, SemanticUnitKind] = {
    "@RestController": SemanticUnitKind.ENDPOINT,
    "@Controller": SemanticUnitKind.ENDPOINT,
    "@Service": SemanticUnitKind.SERVICE,
    "@Component": SemanticUnitKind.SERVICE,
    "@Repository": SemanticUnitKind.REPOSITORY,
    "@Entity": SemanticUnitKind.DATA_MODEL,
    "@Embeddable": SemanticUnitKind.DATA_MODEL,
    "@ConfigurationProperties": SemanticUnitKind.CONFIG,
    "@Configuration": SemanticUnitKind.CONFIG,
    "@Aspect": SemanticUnitKind.MIDDLEWARE,
    "@Interceptor": SemanticUnitKind.MIDDLEWARE,
}

_CLASS_NAME_RE = re.compile(r"\b(?:public\s+)?(?:class|interface|record|enum)\s+(\w+)")

_SKIP_DIR_NAMES = {"target", "build", ".git", "node_modules", "test", "tests"}


class SpringBootSourceAdapter(SourceAdapter):
    stack_key = "springboot"
    display_name = "Spring Boot"

    def discover_files(self, repo_root: str, scope_description: str) -> list[DiscoveredFile]:
        java_root = self._find_java_source_root(repo_root)
        if java_root is None:
            return []

        scope_terms = self._extract_scope_terms(scope_description)

        # Collect every candidate .java file first (path only — content read
        # lazily below, only for files we actually keep).
        candidates: list[tuple[str, str]] = []  # (full_path, relative_path)
        for dirpath, dirnames, filenames in os.walk(java_root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIR_NAMES]
            for filename in filenames:
                if filename.endswith(".java"):
                    full_path = os.path.join(dirpath, filename)
                    candidates.append((full_path, os.path.relpath(full_path, repo_root)))

        selected_paths: set[str]
        if not scope_terms:
            selected_paths = {p for p, _ in candidates}
        else:
            # Pass 1: "anchor" files whose own name directly matches a scope
            # term, e.g. scope "OrderController" matches OrderController.java.
            anchor_dirs = {
                os.path.dirname(full_path)
                for full_path, rel in candidates
                if self._matches_scope(rel, os.path.basename(full_path), scope_terms)
            }
            # Pass 2: pull in every file that lives in the same package
            # directory as an anchor. Spring Boot modules are conventionally
            # one-package-per-feature (controller/service/repository/entity
            # siblings), so this is what lets "migrate OrderController" also
            # sweep in OrderService/OrderRepository without the caller having
            # to name every class explicitly.
            selected_paths = {full_path for full_path, _ in candidates if os.path.dirname(full_path) in anchor_dirs}

        discovered: list[DiscoveredFile] = []
        for full_path, relative_path in candidates:
            if full_path not in selected_paths:
                continue
            content = self._read_file(full_path)
            if content is None:
                continue
            discovered.append(
                DiscoveredFile(
                    path=full_path,
                    relative_path=relative_path,
                    likely_kind=self._guess_kind(content),
                    content=content,
                )
            )

        return discovered

    def build_extraction_prompt(self, file: DiscoveredFile) -> str:
        return f"""You are analyzing a Spring Boot source file. Extract its semantic
content into the IR schema. Spring-specific mapping notes:

- `@RestController` / `@RequestMapping` methods -> one EndpointUnit per HTTP-mapped
  method (`@GetMapping`, `@PostMapping`, etc). The class-level `@RequestMapping`
  path prefix must be combined with the method-level path.
- `@PathVariable` -> parameter source_location "path param".
  `@RequestParam` -> "query param". `@RequestBody` -> the request_body_model.
- `@PreAuthorize` / `@Secured` / Spring Security method annotations -> populate
  `auth_requirement` in plain language (e.g. "requires ROLE_ADMIN").
- `@Service` / `@Component` classes -> ServiceUnit. List constructor-injected
  dependencies (other services/repositories) in `depends_on`.
- `@Repository` interfaces (including Spring Data JPA interfaces like
  `JpaRepository<Order, Long>`) -> RepositoryUnit. Derived query methods
  (e.g. `findByEmailAndActiveTrue`) should each become one plain-language entry
  in `operations`.
- `@Entity` classes -> DataModelUnit. JPA relationship annotations
  (`@OneToMany`, `@ManyToOne`, etc) -> plain-language entries in `relationships`,
  e.g. "has many OrderItem" / "belongs to Account". Bean Validation annotations
  (`@NotNull`, `@Size`, etc) -> entries in the field's `constraints`.
- Put anything Spring-idiomatic that doesn't map cleanly onto the IR schema
  (e.g. `@Transactional` isolation level, `@Async`, AOP details) into
  `stack_specific_hints` rather than forcing it into a first-class field.
- If a referenced type (e.g. a service this class calls, or a repository's
  target entity) is defined in another file you haven't seen, refer to it by
  its plain class name in the relevant field (e.g. `calls_services: ["OrderService"]`)
  — a later resolution pass links these by name, you don't need to resolve them.

File: {file.relative_path}

```java
{file.content}
```
"""

    def guess_unit_name(self, file: DiscoveredFile) -> str:
        match = _CLASS_NAME_RE.search(file.content)
        if match:
            return match.group(1)
        # fall back to filename without extension
        return os.path.splitext(os.path.basename(file.path))[0]

    # ---- internal helpers ----

    @staticmethod
    def _find_java_source_root(repo_root: str) -> str | None:
        candidate = os.path.join(repo_root, "src", "main", "java")
        return candidate if os.path.isdir(candidate) else None

    @staticmethod
    def _extract_scope_terms(scope_description: str) -> list[str]:
        """
        Very deliberately dumb: pulls capitalized word-tokens out of the
        scope description (likely class/module name fragments) rather
        than trying to be clever with NLP. e.g. "migrate the
        OrderController module only" -> ["OrderController"]. Empty
        result means no filtering (whole discovered tree is in scope).
        """
        return re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", scope_description)

    @staticmethod
    def _matches_scope(relative_path: str, filename: str, scope_terms: list[str]) -> bool:
        haystack = relative_path.lower()
        return any(term.lower() in haystack for term in scope_terms)

    @staticmethod
    def _guess_kind(content: str) -> SemanticUnitKind | None:
        for annotation, kind in _ANNOTATION_TO_KIND.items():
            if annotation in content:
                return kind
        return None

    @staticmethod
    def _read_file(path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (UnicodeDecodeError, OSError):
            return None
