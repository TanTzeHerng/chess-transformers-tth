from pathlib import Path

p = Path("pkg/Searchless-SF16-Hybrid-UCI.py")
s = p.read_text(encoding="utf-8")

s = s.replace(
    'ENGINE_NAME = "Searchless + Stockfish16 HCE Hybrid 2.0"',
    'ENGINE_NAME = "Searchless + Stockfish16 HCE Hybrid 2.2"'
)

old_dirs = '''RUN_DIR = pathlib.Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else pathlib.Path(__file__).resolve().parent
MODEL_DIR = RUN_DIR / "models"
SF_DIR = RUN_DIR / "stockfish16"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
SF_DIR.mkdir(parents=True, exist_ok=True)
'''

new_dirs = '''RUN_DIR = pathlib.Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else pathlib.Path(__file__).resolve().parent
BUNDLE_DIR = pathlib.Path(getattr(sys, "_MEIPASS", RUN_DIR)).resolve()
LOCAL_DATA_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", str(RUN_DIR))) / "SearchlessSF16Hybrid"
MODEL_DIR = RUN_DIR / "models"
SF_DIR = RUN_DIR / "stockfish16"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
try:
    SF_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
'''

if old_dirs not in s:
    raise RuntimeError("RUN_DIR block not found")
s = s.replace(old_dirs, new_dirs)

old_find = '''    def _find_binary(self) -> pathlib.Path:
        if self.custom_path:
            p = pathlib.Path(self.custom_path)
            if p.is_file():
                return p
            raise FileNotFoundError(f"HCE Path does not exist: {p}")

        candidates = []
        if os.name == "nt":
            candidates += list(SF_DIR.rglob("stockfish*.exe"))
        else:
            candidates += [p for p in SF_DIR.rglob("stockfish*") if p.is_file() and os.access(p, os.X_OK)]
        if candidates:
            candidates.sort(key=lambda p: ("x86-64" not in p.name.lower(), len(str(p))))
            return candidates[0]

        if os.name != "nt":
            raise FileNotFoundError(
                "Stockfish 16 HCE binary not found. Set UCI option HCE Path to an sf_16 binary."
            )

        archive = SF_DIR / "stockfish-windows-x86-64.zip"
        download_file(SF16_WINDOWS_URL, archive, "official Stockfish 16")
        with zipfile.ZipFile(archive) as z:
            z.extractall(SF_DIR)
        try:
            archive.unlink()
        except OSError:
            pass
        candidates = list(SF_DIR.rglob("stockfish*.exe"))
        if not candidates:
            raise FileNotFoundError("Downloaded Stockfish 16 archive contained no executable")
        candidates.sort(key=lambda p: len(str(p)))
        return candidates[0]
'''

new_find = '''    def _candidate_roots(self) -> list[pathlib.Path]:
        # GUIs such as Lucas Chess may start us from an unrelated working
        # directory. Search relative to the actual executable/bundle first.
        roots = [
            SF_DIR,
            RUN_DIR / "stockfish16",
            RUN_DIR / "_internal" / "stockfish16",
            BUNDLE_DIR / "stockfish16",
            LOCAL_DATA_DIR / "stockfish16",
            pathlib.Path.cwd() / "stockfish16",
        ]
        out: list[pathlib.Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                key = str(root.resolve()).lower()
            except OSError:
                key = str(root).lower()
            if key not in seen:
                seen.add(key)
                out.append(root)
        return out

    def _scan_roots(self) -> list[pathlib.Path]:
        candidates: list[pathlib.Path] = []
        for root in self._candidate_roots():
            if not root.exists():
                continue
            try:
                if os.name == "nt":
                    candidates.extend(p for p in root.rglob("stockfish*.exe") if p.is_file())
                else:
                    candidates.extend(
                        p for p in root.rglob("stockfish*")
                        if p.is_file() and os.access(p, os.X_OK)
                    )
            except OSError:
                continue
        candidates = list(dict.fromkeys(candidates))
        candidates.sort(key=lambda p: (
            "stockfish-windows-x86-64.exe" != p.name.lower(),
            "x86-64" not in p.name.lower(),
            len(str(p)),
        ))
        return candidates

    def _find_binary(self) -> pathlib.Path:
        if self.custom_path:
            p = pathlib.Path(os.path.expandvars(os.path.expanduser(self.custom_path)))
            if p.is_file():
                return p.resolve()
            raise FileNotFoundError(f"HCE Path does not exist: {p}")

        candidates = self._scan_roots()
        if candidates:
            return candidates[0]

        if os.name != "nt":
            searched = "; ".join(str(x) for x in self._candidate_roots())
            raise FileNotFoundError(
                f"Stockfish 16 HCE binary not found. Searched: {searched}"
            )

        # Use a writable per-user fallback instead of assuming that the engine
        # folder is writable when a GUI launches us.
        target = LOCAL_DATA_DIR / "stockfish16"
        target.mkdir(parents=True, exist_ok=True)
        archive = target / "stockfish-windows-x86-64.zip"
        try:
            download_file(SF16_WINDOWS_URL, archive, "official Stockfish 16")
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
        finally:
            try:
                archive.unlink()
            except OSError:
                pass

        candidates = self._scan_roots()
        if not candidates:
            searched = "; ".join(str(x) for x in self._candidate_roots())
            raise FileNotFoundError(
                f"Downloaded Stockfish 16 but no executable was found. Searched: {searched}"
            )
        return candidates[0]
'''

if old_find not in s:
    raise RuntimeError("_find_binary block not found")
s = s.replace(old_find, new_find)
p.write_text(s, encoding="utf-8")
print("patched", p)
