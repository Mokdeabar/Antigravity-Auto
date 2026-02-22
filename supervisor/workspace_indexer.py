"""
workspace_indexer.py — V12 The Omniscient Eye.

Runs in the background to AST-parse the entire active workspace (Python, JS/TS).
Maps all files, classes, and top-level functions into `.ag-supervisor/workspace_map.json`.
Provides `get_relevant_signatures(context_text)` to instantly find related code
during AI analysis without manual file grepping.
"""

import ast
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Any

from . import config

logger = logging.getLogger("supervisor.workspace_indexer")

# Extractor regex for JS/TS (since Python has AST built-in)
# Very naive but effective enough for typical signatures
JS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_]+)", re.MULTILINE)
JS_FUNC_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z0-9_]+)|(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_]+)\s*=>)",
    re.MULTILINE
)


class WorkspaceMap:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.state_dir = config.get_state_dir()
        if not self.state_dir:
            self.state_dir = self.project_path / ".ag-supervisor"
            self.state_dir.mkdir(parents=True, exist_ok=True)
            
        self.map_file = self.state_dir / "workspace_map.json"
        self.index: Dict[str, Dict[str, Any]] = {}

    def _should_ignore(self, path: Path) -> bool:
        """Skip massive generated or untracked directories."""
        ignore_dirs = {
            "node_modules", ".git", ".venv", "venv", "__pycache__", 
            "build", "dist", ".next", ".ag-supervisor", ".gemini"
        }
        for parent in path.parents:
            if parent.name in ignore_dirs:
                return True
        return path.name in ignore_dirs

    def scan_workspace(self) -> None:
        """Fully scan the workspace and build the index."""
        logger.info("👁️  Omniscient Eye starting AST scan of workspace: %s", self.project_path)
        
        self.index.clear()
        
        # Walk the directory
        for root, dirs, files in os.walk(self.project_path):
            root_path = Path(root)
            
            # Prune ignored dirs in-place to avoid decending into them
            dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d)]
            
            for file in files:
                file_path = root_path / file
                if self._should_ignore(file_path):
                    continue
                    
                rel_path = str(file_path.relative_to(self.project_path))
                
                # Parse depending on extension
                if file.endswith(".py"):
                    self._parse_python(file_path, rel_path)
                elif file.endswith((".js", ".ts", ".jsx", ".tsx")):
                    self._parse_jsts(file_path, rel_path)
                    
        self._save()
        logger.info("👁️  Omniscient Eye indexed %d files.", len(self.index))

    def _parse_python(self, path: Path, rel_path: str) -> None:
        """Extract classes and functions using ast."""
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content)
            
            classes = []
            functions = []
            dependencies = [] # V14 Semantic RAG: Bi-Directional imports
            
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    classes.append({
                        "name": node.name,
                        "line": node.lineno
                    })
                elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    functions.append({
                        "name": node.name,
                        "line": node.lineno
                    })
                # V14 AGI: Catch all imports to build the Dependency Graph
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        dependencies.append(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    dependencies.append(node.module)
            
            if classes or functions or dependencies:
                self.index[rel_path] = {
                    "classes": classes,
                    "functions": functions,
                    "dependencies": list(set(dependencies))
                }
                
        except SyntaxError:
            pass # Ignore malformed python files
        except Exception as e:
            logger.debug("Failed to scan Python file %s: %s", rel_path, e)

    def _parse_jsts(self, path: Path, rel_path: str) -> None:
        """Extract classes and functions via regex (best effort)."""
        try:
            content = path.read_text(encoding="utf-8")
            
            classes = []
            functions = []
            
            # Very basic string slicing to get line numbers
            lines = content.splitlines()
            
            for i, line in enumerate(lines):
                c_match = JS_CLASS_RE.search(line)
                if c_match:
                    classes.append({
                        "name": c_match.group(1),
                        "line": i + 1
                    })
                    continue
                
                f_match = JS_FUNC_RE.search(line)
                if f_match:
                    name = f_match.group(1) or f_match.group(2)
                    if name:
                        functions.append({
                            "name": name,
                            "line": i + 1
                        })
            
            if classes or functions:
                self.index[rel_path] = {
                    "classes": classes,
                    "functions": functions
                }

        except Exception as e:
            logger.debug("Failed to scan JS file %s: %s", rel_path, e)

    def _save(self) -> None:
        """Persist the index."""
        try:
            self.map_file.write_text(json.dumps(self.index, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error("👁️  Failed to save workspace map: %s", e)

    def load(self) -> bool:
        """Load the index from disk if it exists."""
        if self.map_file.exists():
            try:
                self.index = json.loads(self.map_file.read_text(encoding="utf-8"))
                return True
            except Exception:
                pass
        return False

    def query_relevant_signatures(self, context_text: str, top_k: int = 5) -> str:
        """
        Dynamically surface file paths and signatures relevant to keywords 
        found in the context text (logs, chat, errors).
        """
        if not self.index:
            self.load()
            if not self.index:
                return ""

        # Extract words from the query that look like code symbols
        # (e.g. CamelCase or snake_case >= 4 chars)
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', context_text)
        unique_words = set(words)
        
        scores: Dict[str, Dict] = {} # rel_path -> {score, matches[]}
        
        for rel_path, data in self.index.items():
            score = 0
            matches = []
            
            # File name match is a huge signal
            base_name = Path(rel_path).stem
            if base_name in unique_words:
                score += 10
                matches.append(f"FileName:{base_name}")
            
            for c in data.get("classes", []):
                if c["name"] in unique_words:
                    score += 5
                    matches.append(f"Class:{c['name']}")
                    
            for f in data.get("functions", []):
                if f["name"] in unique_words:
                    score += 3
                    matches.append(f"Func:{f['name']}")

            if score > 0:
                scores[rel_path] = {
                    "score": score,
                    "matches": list(set(matches)),
                    "data": data
                }

        if not scores:
            return ""

        # Sort by score descending
        sorted_paths = sorted(scores.keys(), key=lambda path: (-scores[path]["score"], path))
        
        out = "## 👁️ THE OMNISCIENT EYE (WORKSPACE RAG V14)\n\n"
        out += "The following files contain structural symbols related to the current context:\n\n"
        
        # V14 Semantic Depth Traversal Context with Cycle Breaking
        MAX_DEPTH = config.MAX_GRAPH_DEPTH if hasattr(config, 'MAX_GRAPH_DEPTH') else 1
        
        for p in sorted_paths[:top_k]:
            meta = scores[p]
            out += f"### {p} (Relevance Score: {meta['score']})\n"
            out += f"  Matched Symbols: {', '.join(meta['matches'])}\n"
            
            if meta['data'].get('classes'):
                out += "  Classes: " + ", ".join(c['name'] for c in meta['data']['classes']) + "\n"
            if meta['data'].get('functions'):
                out += "  Functions: " + ", ".join(f['name'] for f in meta['data']['functions'][:10])
                if len(meta['data']['functions']) > 10:
                    out += " ... (truncated)"
                out += "\n"
            
            # DFS fetch for semantic children
            deps = meta['data'].get('dependencies', [])
            if deps:
                out += f"  Semantic Dependencies (Depth={MAX_DEPTH}):\n"
                
                # Setup DFS stack [dependency_name, current_depth]
                stack = [(d, 1) for d in deps]
                seen = set(deps) # Cycle breaker
                
                child_outputs = []
                while stack:
                    curr_dep, curr_depth = stack.pop()
                    if curr_depth > MAX_DEPTH:
                        continue
                    
                    child_outputs.append(f"    {'  ' * curr_depth}↳ {curr_dep}")
                    
                    # If this dependency maps to a known local file index (fuzzy match)
                    for idx_path, idx_data in self.index.items():
                        if curr_dep in idx_path.replace("\\", ".").replace("/", "."):
                            nested_deps = idx_data.get("dependencies", [])
                            for nd in nested_deps:
                                if nd not in seen:
                                    seen.add(nd) # Break cyclical imports instantly
                                    stack.append((nd, curr_depth + 1))
                                    
                if child_outputs:
                    # Truncate to prevent enormous tree splats
                    out += "\n".join(child_outputs[:15])
                    if len(child_outputs) > 15:
                        out += f"\n    ... (+{len(child_outputs)-15} more children truncated)"
                    out += "\n"

            out += "\n"

        return out.strip()
