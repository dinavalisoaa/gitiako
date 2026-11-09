import json, os, re, shutil, subprocess, sys, tempfile, threading, tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

MAX_DIFF, MAX_FILE, MAX_LINES = 40000, 4000, 3000  # MAX_DIFF/MAX_FILE : taille envoyée à l'IA ; MAX_LINES : aperçu
CONF_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "CommitMsg")  # hors de l'exe
CONF = os.path.join(CONF_DIR, "repos.json")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
repos = []      # repos connus, le plus récent en premier
repo, branch = "", ""
ahead = behind = None
files = []      # (xy, path, [paths à committer: new (+ old si rename)])
checked = set()
last = {"k": None}  # dernier fichier affiché dans l'aperçu


def run(cmd, **kw):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # échoue au lieu de bloquer sur un prompt
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       stdin=subprocess.DEVNULL, creationflags=NOWIN, **kw)  # stdin explicite : requis dans un exe sans console
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} exit {r.returncode}")
    return r.stdout


def git(*a):
    return run(["git", "-C", repo, *a])


# ---------- IA : un processus claude déjà démarré (l'init de ~8 s se fait pendant que tu coches) ----------
SYS = "You write git commit messages in plain prose. Output ONLY the message: no code fences, no preamble."
CLAUDE = [shutil.which("claude") or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
          "--verbose", "--no-session-persistence", "--model", "haiku", "--effort", "low", "--tools", "",
          "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "project", "--system-prompt", SYS]
warm, warm_lock = None, threading.Lock()


def spawn():
    global warm
    with warm_lock:
        if warm is None or warm.poll() is not None:
            warm = subprocess.Popen(CLAUDE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", cwd=tempfile.gettempdir(), creationflags=NOWIN)


def ask(prompt):
    global warm
    spawn()  # sans effet si déjà chaud
    with warm_lock:
        p, warm = warm, None
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
    p.stdin.flush()
    res = None
    for line in p.stdout:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "result":
            res = d
            break
    p.stdin.close()
    threading.Thread(target=spawn, daemon=True).start()  # chauffe le suivant
    if not res:
        raise RuntimeError("claude s'est arrêté sans réponse (connecté ? lance `claude` dans un terminal)")
    if res.get("is_error"):
        raise RuntimeError(res.get("result") or "erreur claude")
    return res["result"]


# ---------- coloration syntaxique (par extension, sans dépendance) ----------
def _kw(words):
    return r"\b(?:" + "|".join(words.split()) + r")\b"


KW_C = ("abstract assert boolean break byte case catch char class const continue default do double else enum extends "
        "final finally float for if implements import instanceof int interface long native new null package private "
        "protected public return short static super switch synchronized this throw throws transient try void volatile "
        "while true false var val fun let function export async await yield typeof delete in of record sealed permits "
        "override lateinit fn mut impl pub struct trait self go defer chan func namespace using string bool readonly "
        "undefined")
KW_PY = ("and as assert async await break class continue def del elif else except finally for from global if import in "
         "is lambda None nonlocal not or pass raise return try while with yield True False self")
KW_SQL = ("select from where and or not insert into values update set delete create alter drop table index primary key "
          "foreign references null default join left right inner outer on group by order having limit offset union all "
          "distinct as case when then else end add column constraint unique varchar int bigint text boolean timestamp "
          "date if exists engine charset")
KW_SH = "if then else elif fi for while do done case esac function in echo exit return export local set unset cd"
STR2 = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
NUM = r"\b0[xX][0-9a-fA-F_]+\b|\b\d[\d_]*(?:\.\d+)?[fFdDlL]?\b"
TYP = r"\b[A-Z][A-Za-z0-9_]*\b"
PATS = {  # famille -> (regex à groupes nommés = classes de jetons, flags)
    "clike": (rf"(?P<com>//.*|/\*.*?\*/|/\*.*$)|(?P<str>{STR2}|`(?:\\.|[^`\\])*`)|(?P<ann>@\w+)|(?P<num>{NUM})"
              rf"|(?P<kw>{_kw(KW_C)})|(?P<typ>{TYP})", 0),
    "py": (rf"(?P<com>#.*)|(?P<str>{STR2})|(?P<ann>@\w+)|(?P<num>{NUM})|(?P<kw>{_kw(KW_PY)})|(?P<typ>{TYP})", 0),
    "sql": (rf"(?P<com>--.*|/\*.*?\*/|/\*.*$)|(?P<str>'(?:''|[^'])*')|(?P<num>{NUM})|(?P<kw>{_kw(KW_SQL)})", re.I),
    "xml": (r'(?P<com><!--.*?-->|<!--.*$)|(?P<str>(?<==)"[^"]*"|(?<==)\'[^\']*\')|(?P<tag></?[\w:.-]+|/?>)'
            r'|(?P<attr>[\w:.-]+(?==))', 0),
    "yaml": (rf"(?P<com>#.*)|(?P<str>{STR2})|(?P<attr>(?<![^\s-])[\w.\-/]+(?=\s*:(?:\s|$)))"
             rf"|(?P<num>\b\d+(?:\.\d+)?\b)|(?P<kw>\b(?:true|false|null|yes|no)\b)", 0),
    "json": (r'(?P<attr>"(?:\\.|[^"\\])*"(?=\s*:))|(?P<str>"(?:\\.|[^"\\])*")|(?P<num>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)'
             r'|(?P<kw>\b(?:true|false|null)\b)', 0),
    "props": (rf"(?P<com>^\s*[#;!].*)|(?P<attr>^\s*[\w.\-\[\]/]+(?=\s*[=:]))|(?P<str>\"[^\"]*\")|(?P<num>\b\d+\b)", 0),
    "sh": (rf"(?P<com>(?<![\w$])#.*)|(?P<str>{STR2})|(?P<ann>\$\{{?\w+\}}?)|(?P<num>\b\d+\b)|(?P<kw>{_kw(KW_SH)})", 0),
    "css": (rf"(?P<com>/\*.*?\*/|/\*.*$)|(?P<str>{STR2})|(?P<attr>[\w-]+(?=\s*:))|(?P<ann>[.#][A-Za-z_][\w-]*)"
            rf"|(?P<num>-?\b\d+(?:\.\d+)?(?:px|em|rem|vh|vw|ms|s)?\b)", 0),
}
BLOCK = {"clike", "css", "sql"}  # familles avec commentaires /* */ multi-lignes
LANG = {}
for _fam, _exts in {"clike": "java kt kts scala groovy gradle c h cpp hpp cc cs go rs js jsx mjs cjs ts tsx php swift dart",
                    "py": "py", "sql": "sql", "xml": "xml html htm xhtml jsp jspx vue svg xsd xsl fxml",
                    "yaml": "yml yaml", "json": "json", "props": "properties ini toml conf cfg env",
                    "sh": "sh bash zsh bat cmd ps1", "css": "css scss less"}.items():
    LANG.update({e: _fam for e in _exts.split()})
_rx = {}


def lang_of(title):  # "chemin" ou "hash · chemin"
    ext = os.path.splitext(title.rsplit(" · ", 1)[-1])[1].lower().lstrip(".")
    return LANG.get(ext)


def toks(line, fam, st):  # [(texte, classe|None)] ; st["b"] : on est dans un commentaire /* */ commencé plus haut
    if fam not in _rx:
        _rx[fam] = re.compile(*PATS[fam])
    out, pos = [], 0
    if len(line) > 1500:
        return [(line, None)]
    if fam in BLOCK and not st["b"] and re.match(r"\s*\*(?!\w)", line):  # suite de Javadoc dont l'ouverture est hors aperçu
        return [(line, "com")]
    if st["b"]:
        j = line.find("*/")
        if j < 0:
            return [(line, "com")]
        out.append((line[:j + 2], "com"))
        pos, st["b"] = j + 2, False
    for m in _rx[fam].finditer(line, pos):
        if m.start() > pos:
            out.append((line[pos:m.start()], None))
        out.append((m.group(), m.lastgroup))
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], None))
    if fam in BLOCK and out and out[-1][1] == "com":
        st["b"] = out[-1][0].startswith("/*") and not out[-1][0].endswith("*/")
    return out


def put(line, kind, fam, st):  # insère une ligne de diff : signe, puis le code coloré
    base = (kind,) if kind else ()
    d.insert(tk.END, line[:1], base)
    if fam:
        for s, tg in toks(line[1:], fam, st):
            d.insert(tk.END, s, base + ((tg,) if tg else ()))
    else:
        d.insert(tk.END, line[1:], base)
    d.insert(tk.END, "\n", base)


# ---------- aperçu du diff ----------
HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)")


def show_diff(text, title):
    diff_title.set(title or "Sélectionne un fichier pour voir ses changements")
    d.config(state=tk.NORMAL)
    d.delete("1.0", tk.END)
    o = n = na = nd = 0
    fam, st = lang_of(title or ""), {"b": False}
    inh = False  # dans un hunk : les lignes +++/--- des en-têtes ne sont pas du contenu
    for k, line in enumerate(text.splitlines()):
        if k > MAX_LINES:
            d.insert(tk.END, "… diff tronqué\n", "meta")
            break
        if line.startswith("diff --git"):
            inh = False
            continue
        m = HUNK.match(line) if line.startswith("@@") else None
        if m:
            inh, o, n, st["b"] = True, int(m[1]), int(m[2]), False
            d.insert(tk.END, line + "\n", "hunk")
        elif not inh:
            if line.startswith("Binary files"):
                d.insert(tk.END, "Fichier binaire : pas d'aperçu\n", "meta")
        elif line[:1] == "+":
            d.insert(tk.END, f"{'':>5} {n:>5}  ", ("add", "ln"))
            put(line, "add", fam, st)
            n += 1
            na += 1
        elif line[:1] == "-":
            d.insert(tk.END, f"{o:>5} {'':>5}  ", ("del", "ln"))
            put(line, "del", fam, st)
            o += 1
            nd += 1
        elif line[:1] == "\\":
            d.insert(tk.END, line + "\n", "meta")
        else:
            d.insert(tk.END, f"{o:>5} {n:>5}  ", "ln")
            put(line, None, fam, st)
            o += 1
            n += 1
    if d.index("end-1c") == "1.0":
        d.insert(tk.END, "Aucun contenu à afficher" if title else "Sélectionne un fichier pour voir ses changements", "empty")
    diff_add.set(f"+{na}" if na else "")
    diff_del.set(f"−{nd}" if nd else "")
    d.config(state=tk.DISABLED)


def synth(f):  # fichier non suivi : tout est ajouté
    with open(os.path.join(repo, f), "rb") as fh:
        raw = fh.read(200000)
    if b"\0" in raw:
        return "Binary files differ"
    lines = raw.decode("utf-8", "replace").splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines)


def preview(iid):
    ent = next((x for x in files if x[1] == iid), None)
    if not ent or last["k"] == iid:
        return
    last["k"] = iid
    xy, f, ps = ent
    try:
        if xy == "??":
            text = synth(f)
        else:
            try:
                text = git("diff", "HEAD", "-U3", "--no-color", "--", *ps)
            except RuntimeError:  # repo sans commit
                text = git("diff", "--cached", "-U3", "--no-color", "--", *ps)
    except (RuntimeError, OSError) as e:
        text = ""
        status.set(str(e))
    show_diff(text, f)


def on_tree_select(_):
    sel = tree.selection()
    if len(sel) == 1:
        preview(sel[0])


# ---------- fichiers modifiés ----------
SYM = {"??": "＋", "A": "＋", "D": "－", "R": "➜", "C": "➜"}
TAG = {"＋": "new", "－": "del", "➜": "ren"}


def load():
    global files, branch
    files, branch = [], ""
    checked.clear()
    last["k"] = None
    show_diff("", "")
    htree.delete(*htree.get_children())
    ftree.delete(*ftree.get_children())
    bcombo["values"] = []
    bcombo.set("")
    if repo:
        try:
            parts = git("status", "--porcelain", "-z", "-uall").split("\0")
            branch = git("branch", "--show-current").strip() or "HEAD"
        except RuntimeError as e:
            parts = []
            messagebox.showerror("Erreur", str(e))
        i = 0
        while i < len(parts):
            p = parts[i]
            i += 1
            if not p:
                continue
            xy, f = p[:2], p[3:]
            ps = [f]
            if xy[0] in "RC":
                ps.append(parts[i])
                i += 1
            files.append((xy, f, ps))
        meta()  # rien n'est coché par défaut (ni au chargement, ni après un commit)
    root.title(f"Commit — {repo}" if repo else "Commit")
    render()


def meta():  # branches, avance/retard sur le distant, historique
    global ahead, behind
    try:
        loc = git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
        rem = [r.split("/", 1)[1] for r in git("for-each-ref", "--format=%(refname:short)", "refs/remotes").split()
               if "/" in r]  # "origin/HEAD" s'affiche "origin" : sans slash, donc écarté
        bcombo["values"] = loc + [r for r in rem if r not in loc]
        bcombo.set(branch)
    except RuntimeError:
        pass
    try:
        behind, ahead = map(int, git("rev-list", "--left-right", "--count", "@{u}...HEAD").split())
    except (RuntimeError, ValueError):  # pas d'upstream
        ahead = behind = None
    tb_push.config(text="Push" + (f" ↑{ahead}" if ahead else ""))
    tb_pull.config(text="Pull" + (f" ↓{behind}" if behind else ""))
    try:
        for line in git("log", "-300", "--pretty=format:%h%x1f%s%x1f%an%x1f%ar").splitlines():
            h, s, a, dt = line.split("\x1f")
            htree.insert("", tk.END, iid=h, values=(s, a, dt))
    except RuntimeError:  # aucun commit
        pass


def render(*_):
    q = flt.get().lower()
    tree.delete(*tree.get_children())
    for xy, f, _ in files:
        if q in f.lower():
            s = SYM.get(xy.strip(), "●")
            tree.insert("", tk.END, iid=f, text="", image=IMG["on" if f in checked else "off"], values=(s, f),
                        tags=(TAG.get(s, "mod"),))
    counts()


def counts():
    vis = tree.get_children()
    on = sum(v in checked for v in vis)
    tree.heading("#0", image=IMG["on" if vis and on == len(vis) else "mixed" if on else "off"])  # case d'en-tête tri-état
    n = len(checked)
    hidden = n - on  # cochés mais masqués par le filtre : ils seront quand même commités
    count_lbl.set(f"{len(files)} fichiers · {n} cochés" + (f" (dont {hidden} masqués)" if hidden else ""))
    commit_btn.config(text=f"Commit {n} → {branch or '…'}")
    tab_chg.config(text=f"Changements ({len(files)})")


def setstate(iids, on):
    for i in iids:
        (checked.add if on else checked.discard)(i)
        tree.item(i, image=IMG["on" if on else "off"])
    counts()


def toggle_all():  # clic sur l'en-tête : tout cocher, ou tout décocher si déjà tout coché
    vis = tree.get_children()
    setstate(vis, not all(v in checked for v in vis))


def invert():
    vis = tree.get_children()
    on = [v for v in vis if v in checked]
    setstate([v for v in vis if v not in checked], True)
    setstate(on, False)


anchor, paint = None, None   # ancre du Maj+clic ; état appliqué pendant un glisser
held = {"shift": False}      # Maj suivi aux touches, en plus de e.state


def span(a, b, on):  # coche/décoche la plage visible entre deux lignes et la surligne
    vis = tree.get_children()
    i, j = sorted((vis.index(a), vis.index(b)))
    setstate(vis[i:j + 1], on)
    tree.selection_set(vis[i:j + 1])


def press(e):
    global anchor, paint
    if tree.identify_region(e.x, e.y) not in ("cell", "tree"):  # en-tête : laisse son propre handler
        return
    iid = tree.identify_row(e.y)
    tree.focus_set()
    vis = tree.get_children()
    base = anchor if anchor in vis else tree.focus()
    if (e.state & 0x1 or held["shift"]) and base in vis:  # Maj+clic : plage depuis l'ancre, avec l'état de l'ancre
        span(base, iid, base in checked)
        paint = None
    elif tree.identify_column(e.x) == "#0":  # clic sur la case : bascule ; glisser applique ce même état
        paint, anchor = iid not in checked, iid
        setstate([iid], paint)
        tree.selection_set(iid)
    else:  # clic ailleurs : sélectionne et affiche l'aperçu
        paint, anchor = None, iid
        tree.selection_set(iid)
    tree.focus(iid)
    preview(iid)
    return "break"


def dbl(e):  # double-clic hors case : bascule la ligne
    iid = tree.identify_row(e.y)
    if iid and tree.identify_column(e.x) != "#0":
        setstate([iid], iid not in checked)


def drag(e):
    iid = tree.identify_row(e.y)
    if paint is not None and iid in tree.get_children() and anchor in tree.get_children():
        span(anchor, iid, paint)


def release(e):
    global paint
    paint = None


def key_toggle(e):  # Espace : bascule la ligne courante (navigation aux flèches)
    global anchor
    iid = tree.focus()
    if iid:
        anchor = iid
        setstate([iid], iid not in checked)
    return "break"


def selected():
    return [x for x in files if x[1] in checked]


# ---------- historique ----------
def on_commit(_):
    sel = htree.selection()
    if not sel:
        return
    ftree.delete(*ftree.get_children())
    try:
        out = git("show", "--first-parent", "-m", "--name-status", "--pretty=format:", "-M", sel[0])
    except RuntimeError as e:
        return status.set(str(e))
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) > 1 and not ftree.exists(p[-1]):
            ftree.insert("", tk.END, iid=p[-1], values=(SYM.get(p[0][0], "●"), p[-1]))
    kids = ftree.get_children()
    if kids:
        ftree.selection_set(kids[0])
    else:
        show_diff("", f"{sel[0]} · aucun fichier")


def on_hfile(_):
    sel, hs = ftree.selection(), htree.selection()
    if sel and hs:
        try:
            out = git("show", "--first-parent", "-m", "-U3", "--no-color", "--pretty=format:", "-M", hs[0], "--", sel[0])
        except RuntimeError as e:
            return status.set(str(e))
        show_diff(out, f"{hs[0]} · {sel[0]}")


def tab(which):
    (hist_frame if which == "chg" else chg_frame).pack_forget()
    (chg_frame if which == "chg" else hist_frame).pack(fill=tk.BOTH, expand=True)
    tab_chg.config(bg=SEL if which == "chg" else BG, fg=FG if which == "chg" else MUTED)
    tab_hist.config(bg=SEL if which == "hist" else BG, fg=FG if which == "hist" else MUTED)
    if which == "chg":
        sel = tree.selection()
        last["k"] = None
        preview(sel[0]) if len(sel) == 1 else show_diff("", "")
    else:
        on_hfile(None) if ftree.selection() else show_diff("", "")


# ---------- tâches de fond ----------
def bg(job, then):
    for b in btns:
        b.config(state=tk.DISABLED)
    pb.place(relwidth=1, relheight=1)
    pb.start(12)

    def t():
        try:
            r, err = job(), None
        except Exception as e:
            r, err = None, str(e)
        root.after(0, lambda: fin(then, r, err))
    threading.Thread(target=t, daemon=True).start()


def fin(then, r, err):
    pb.stop()
    pb.place_forget()
    for b in btns:
        b.config(state=tk.NORMAL)
    if err:
        status.set("Erreur")
        bcombo.set(branch)
        messagebox.showerror("Erreur", err)
    else:
        then(r)


def diff_of(sel):  # un seul appel git, -U1, plafonné par fichier : prompt court = réponse rapide
    tracked = [p for xy, _, ps in sel if xy != "??" for p in ps]
    out = ""
    if tracked:
        try:
            out = git("diff", "HEAD", "-U1", "--no-color", "--", *tracked)
        except RuntimeError:  # repo sans commit
            out = git("diff", "--cached", "-U1", "--no-color", "--", *tracked)
    chunks = [c[:MAX_FILE] for c in re.split(r"(?m)^(?=diff --git )", out) if c]
    for xy, f, _ in sel:
        if xy == "??":
            with open(os.path.join(repo, f), "rb") as fh:
                raw = fh.read(MAX_FILE)
            chunks.append(f"new file: {f}\n" + ("(binaire)" if b"\0" in raw else raw.decode("utf-8", "replace")))
    return "\n".join(chunks)[:MAX_DIFF]


def clean(t):  # jamais de trailer Co-Authored-By dans les commits
    return "\n".join(l for l in t.splitlines() if not l.lower().startswith("co-authored-by")).strip()


VER = (("package.json", r'^\+\s*"version":\s*"([^"]+)"'), ("gradle.properties", r"^\+\s*version\s*=\s*(\S+)"),
       ("pyproject.toml", r'^\+\s*version\s*=\s*"([^"]+)"'), ("Cargo.toml", r'^\+\s*version\s*=\s*"([^"]+)"'),
       ("build.gradle", r"^\+\s*version\s*=?\s*['\"]([^'\"]+)['\"]"))


def version_hint(diff):  # changements de version détectés dans les fichiers de build/manifeste
    found = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        head = chunk.split("\n", 1)[0]
        for name, pat in VER:
            m = head.endswith("/" + name) and re.search(pat, chunk, re.M)
            if m:
                found.append(f"{name} -> {m[1]}")
        if head.endswith("/pom.xml") and re.search(r"^-\s*<version>", chunk, re.M):
            m = re.search(r"^\+\s*<version>([^<$]+)</version>", chunk, re.M)
            if m:  # pom : beaucoup de <version> (dépendances) -> l'IA vérifie que c'est celle du projet
                found.append(f"pom.xml -> {m[1]} (only if it is the project's own version, not a dependency)")
    return "; ".join(found) or "none detected"


def ai():
    sel = selected()
    if not sel:
        return status.set("Sélectionne des fichiers")
    status.set("Génération IA…")

    def job():
        diff = diff_of(sel)
        try:
            log = git("log", "-8", "--pretty=%s")
        except RuntimeError:
            log = ""
        return ask(
            "Write a git commit message for the changes below.\n\n"
            "Subject line format: <type>(<scope>): <Summary> — max 72 chars, imperative mood, no trailing period.\n"
            "- type: feat = new capability; fix = bug fix; hotfix = urgent production fix (use it whenever the branch "
            "name starts with hotfix). Use refactor/test/docs/chore only if the change is clearly none of those.\n"
            "- scope: the main domain model or module the change is about, one short lowercase word "
            "(e.g. application, organization, settlement, merchant, pricing). Never a package path or a file name. "
            "If several are touched, pick the dominant one.\n"
            "- Version bump: if the change is mainly a version bump (package.json, pom.xml, gradle, pyproject…), the "
            "scope is `version X.Y.Z` with the NEW version, e.g. `feat(version 1.4.0): Bump version to 1.4.0`.\n"
            "Examples: `feat(organization): Track legal status changes`, `fix(settlement): Reject negative payout "
            "fee`, `hotfix(application): Restore document upload`.\n"
            "Then a blank line and a description of 1-3 plain sentences (what and why). Write in the language of the "
            "recent commits but ignore their prefix style.\n\n"
            f"Current branch: {branch}\nVersion changes detected: {version_hint(diff)}\n"
            f"Recent commit subjects:\n{log}\nChanges:\n{diff}")

    def then(res):
        head, _, body = clean(res).partition("\n")
        summary.delete(0, tk.END)
        summary.insert(0, head.strip())
        desc.delete("1.0", tk.END)
        desc.insert("1.0", body.strip())
        status.set("Message généré")
    bg(job, then)


def commit(push):
    sel, s, d_ = selected(), clean(summary.get()), clean(desc.get("1.0", tk.END))
    if not sel or not s:
        return messagebox.showwarning("Commit", "Il faut des fichiers cochés et un titre.")
    status.set("Commit + push…" if push else "Commit…")

    def job():
        new = [p for xy, _, ps in sel if xy == "??" for p in ps]
        if new:
            git("add", "--", *new)
        paths = [p for _, _, ps in sel for p in ps]
        git("commit", "-m", s, *(["-m", d_] if d_ else []), "--", *paths)  # --only: ignore le reste de l'index
        if push:
            git("push", "-u", "origin", "HEAD")

    def then(_):
        summary.delete(0, tk.END)
        desc.delete("1.0", tk.END)
        load()
        status.set("Commit + push OK" if push else "Commit OK")
    bg(job, then)


# ---------- branches et synchro ----------
def switch_branch(_=None):
    name = bcombo.get()
    if name and name != branch:
        bg(lambda: git("checkout", name), lambda _: (load(), status.set(f"Branche : {name}")))


def new_branch():
    name = (simpledialog.askstring("Nouvelle branche", f"Nom (créée depuis {branch}) :", parent=root) or "").strip()
    if name:
        bg(lambda: git("checkout", "-b", name), lambda _: (load(), status.set(f"Branche créée : {name}")))


def sync(label, *args):
    if repo:
        status.set(label + "…")
        bg(lambda: git(*args), lambda _: (load(), status.set(label + " OK")))


# ---------- repos mémorisés ----------
def save_conf():
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(CONF, "w", encoding="utf-8") as fh:
            json.dump(repos, fh, indent=1)
    except OSError:
        pass


def names():  # nom du dossier ; chemin complet seulement si doublon
    base = [os.path.basename(p) or p for p in repos]
    return [n if base.count(n) == 1 else p for n, p in zip(base, repos)]


def set_repo(d_):
    global repo
    try:
        top = run(["git", "-C", d_, "rev-parse", "--show-toplevel"]).strip()  # accepte un sous-dossier
    except (RuntimeError, OSError):
        return messagebox.showerror("Erreur", f"Pas un dépôt git :\n{d_}")
    d_ = os.path.normpath(top)
    if d_ in repos:
        repos.remove(d_)
    repos.insert(0, d_)  # le dernier utilisé passe en tête
    repo = d_
    save_conf()
    combo["values"] = names()
    combo.current(0)
    load()


def pick():
    p = filedialog.askdirectory()
    if p:
        set_repo(p)


def remove_repo():  # retire de la liste, ne touche pas au disque
    global repo
    if repo in repos:
        repos.remove(repo)
    save_conf()
    repo = repos[0] if repos else ""
    combo["values"] = names()
    combo.current(0) if repo else combo.set("")
    load()


def quit_app():
    with warm_lock:
        if warm and warm.poll() is None:
            warm.kill()
    root.destroy()


# ---------- interface ----------
BG, TOP, PANEL, FIELD, BORDER = "#161b22", "#1c2128", "#0d1117", "#0d1117", "#30363d"  # volet / barre / zones creuses
FG, MUTED, SEL = "#e6edf3", "#7d8590", "#1f3a5f"
BLUE, GREEN, PURPLE, GREY, RED, AMBER = "#2f81f7", "#2ea043", "#8957e5", "#2b313a", "#f85149", "#d29922"
FONT, FONT_B, MONO = ("Segoe UI", 10), ("Segoe UI Semibold", 10), ("Consolas", 10)


def lighten(c):
    return "#%02x%02x%02x" % tuple(min(255, v + (255 - v) // 8) for v in (int(c[i:i + 2], 16) for i in (1, 3, 5)))


class RBtn(tk.Canvas):  # bouton à coins arrondis ; mêmes options config(text/bg/fg/state) qu'un tk.Button
    def __init__(self, parent, text, cmd, color=GREY, fg=FG, font=FONT, padx=14, pady=7):
        self.fnt, self.padx = tkfont.Font(font=font), padx
        super().__init__(parent, height=self.fnt.metrics("linespace") + 2 * pady, bg=parent["bg"], highlightthickness=0,
                         bd=0, cursor="hand2")
        self.text, self.color, self.fg, self.cmd, self.on, self.hov = text, color, fg, cmd, True, False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self.hover(True))
        self.bind("<Leave>", lambda e: self.hover(False))
        self.bind("<ButtonRelease-1>", self.click)
        self.config(text=text)

    def hover(self, v):
        self.hov = v
        self.draw()

    def click(self, e):
        if self.on and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.cmd()

    def config(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        for k, a in (("text", "text"), ("bg", "color"), ("fg", "fg")):
            if k in kw:
                setattr(self, a, kw.pop(k))
        if "state" in kw:
            self.on = kw.pop("state") != tk.DISABLED
            super().config(cursor="hand2" if self.on else "arrow")
        if kw:
            super().config(**kw)
        super().config(width=self.fnt.measure(self.text) + 2 * self.padx)
        self.draw()
    configure = config

    def draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), int(self.cget("width"))), self.winfo_height()
        r = min(8, h // 2)
        x1, y1, x2, y2 = 1, 1, w - 1, h - 1
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2,
               x1, y2 - r, x1, y1 + r, x1, y1]
        self.create_polygon(pts, smooth=True, outline="", fill=lighten(self.color) if self.hov and self.on else self.color)
        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg if self.on else MUTED, font=self.fnt)


def btn(parent, text, cmd, color=GREY, fg=FG, font=FONT, **kw):
    return RBtn(parent, text, cmd, color, fg, font, **kw)


def field(cls, parent, **kw):
    return cls(parent, bg=FIELD, fg=FG, insertbackground=FG, relief=tk.FLAT, font=FONT, highlightthickness=1,
               highlightbackground=BORDER, highlightcolor=BLUE, **kw)


def label(parent, **kw):
    return tk.Label(parent, bg=kw.pop("bg", BG), fg=kw.pop("fg", FG), font=kw.pop("font", FONT), anchor="w", **kw)


def make_tree(parent, columns, show="headings", **kw):  # Treeview + barre de défilement dans un cadre bordé
    box = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    t = ttk.Treeview(box, columns=columns, show=show, **kw)
    s = ttk.Scrollbar(box, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=s.set)
    s.pack(side=tk.RIGHT, fill=tk.Y)
    t.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return box, t


root = tk.Tk()
root.geometry("1280x840")
root.minsize(1000, 660)
root.configure(bg=BG)
root.protocol("WM_DELETE_WINDOW", quit_app)
st = ttk.Style()
st.theme_use("clam")
st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=30, borderwidth=0, font=FONT,
             bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
st.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0, relief="flat", font=FONT)
st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", FG)])
st.map("Treeview.Heading", background=[("active", BG)])
st.configure("TCombobox", fieldbackground=FIELD, background=GREY, foreground=FG, arrowcolor=FG, bordercolor=BORDER,
             lightcolor=FIELD, darkcolor=FIELD, selectbackground=FIELD, selectforeground=FG, padding=4)
st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)])
for o, side in (("Vertical", "ns"), ("Horizontal", "ew")):  # barres fines, sans flèches
    st.layout(f"{o}.TScrollbar", [(f"{o}.Scrollbar.trough", {"sticky": side, "children": [
        (f"{o}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.configure(f"{o}.TScrollbar", background=BORDER, troughcolor=PANEL, bordercolor=PANEL, lightcolor=BORDER,
                 darkcolor=BORDER, arrowsize=10, relief="flat")
    st.map(f"{o}.TScrollbar", background=[("active", "#4a525e")])
st.configure("Horizontal.TProgressbar", troughcolor=BORDER, background=BLUE, bordercolor=BG, lightcolor=BLUE,
             darkcolor=BLUE, thickness=3)
root.option_add("*TCombobox*Listbox.background", FIELD)
root.option_add("*TCombobox*Listbox.foreground", FG)
root.option_add("*TCombobox*Listbox.selectBackground", SEL)
count_lbl, status, diff_title, diff_add, diff_del = (tk.StringVar() for _ in range(5))


def box_img(kind):  # case à cocher façon GitHub Desktop : carré vide / bleu + coche / bleu + tiret
    im = tk.PhotoImage(width=18, height=18)
    edge, fill = ("#6e7681", PANEL) if kind == "off" else (BLUE, BLUE)
    im.put(edge, to=(2, 2, 16, 16))
    im.put(fill, to=(3, 3, 15, 15))
    for x, y in ((2, 2), (15, 2), (2, 15), (15, 15)):  # coins arrondis
        im.transparency_set(x, y, True)
    if kind == "on":
        pts = [(5 + i, 9 + i) for i in range(3)] + [(8 + i, 11 - i) for i in range(5)]
        for x, y in pts:
            for dy in (0, 1):
                im.put("#ffffff", to=(x, y - dy))
    elif kind == "mixed":
        im.put("#ffffff", to=(5, 8, 13, 10))
    return im


IMG = {k: box_img(k) for k in ("off", "on", "mixed")}  # références gardées : sinon Tk les efface

# barre du haut : dépôt · branche · synchro
tb = tk.Frame(root, bg=TOP, highlightthickness=1, highlightbackground=BORDER)
tb.pack(fill=tk.X)


def section(caption):
    f = tk.Frame(tb, bg=TOP)
    f.pack(side=tk.LEFT, padx=(14, 0), pady=8)
    label(f, text=caption.upper(), bg=TOP, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(fill=tk.X, pady=(0, 2))
    r = tk.Frame(f, bg=TOP)
    r.pack()
    return r


r1 = section("Dépôt")
combo = ttk.Combobox(r1, state="readonly", font=FONT_B, width=26)
combo.pack(side=tk.LEFT)
combo.bind("<<ComboboxSelected>>", lambda e: set_repo(repos[combo.current()]))
btn(r1, "＋", pick, padx=8, pady=3).pack(side=tk.LEFT, padx=(6, 0))
btn(r1, "✕", remove_repo, padx=8, pady=3).pack(side=tk.LEFT, padx=(4, 0))
r2 = section("Branche")
bcombo = ttk.Combobox(r2, state="readonly", font=FONT_B, width=26)
bcombo.pack(side=tk.LEFT)
bcombo.bind("<<ComboboxSelected>>", switch_branch)
btn(r2, "＋", new_branch, padx=8, pady=3).pack(side=tk.LEFT, padx=(6, 0))
r3 = tk.Frame(tb, bg=TOP)
r3.pack(side=tk.RIGHT, padx=14, pady=12)
tb_push = btn(r3, "Push", lambda: sync("Push", "push", "-u", "origin", "HEAD"), padx=12, pady=5)
tb_push.pack(side=tk.RIGHT, padx=(6, 0))
tb_pull = btn(r3, "Pull", lambda: sync("Pull", "pull", "--ff-only"), padx=12, pady=5)
tb_pull.pack(side=tk.RIGHT, padx=(6, 0))
tb_fetch = btn(r3, "Fetch", lambda: sync("Fetch", "fetch", "--prune"), padx=12, pady=5)
tb_fetch.pack(side=tk.RIGHT)

pw = tk.PanedWindow(root, orient=tk.HORIZONTAL, bg=BORDER, sashwidth=3, bd=0, sashrelief=tk.FLAT)
pw.pack(fill=tk.BOTH, expand=True)
left, right = tk.Frame(pw, bg=BG), tk.Frame(pw, bg=BG)
pw.add(left, minsize=400, width=480)
pw.add(right, minsize=320, stretch="always")

# --- volet gauche : onglets
label(left, textvariable=status, fg=MUTED).pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(2, 8))
pbox = tk.Frame(left, bg=BG, height=3)  # réserve la place : la barre n'apparaît que pendant une tâche
pbox.pack(side=tk.BOTTOM, fill=tk.X, padx=12)
pb = ttk.Progressbar(pbox, mode="indeterminate")
tabs = tk.Frame(left, bg=BG)
tabs.pack(fill=tk.X, padx=12, pady=(10, 6))
tab_chg = btn(tabs, "Changements", lambda: tab("chg"), SEL, FG, FONT_B, pady=6)
tab_chg.pack(side=tk.LEFT, fill=tk.X, expand=True)
tab_hist = btn(tabs, "Historique", lambda: tab("hist"), BG, MUTED, FONT_B, pady=6)
tab_hist.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

# --- onglet Changements
chg_frame = tk.Frame(left, bg=BG)
chg_frame.pack(fill=tk.BOTH, expand=True)
fbar = tk.Frame(chg_frame, bg=BG)
fbar.pack(fill=tk.X, padx=12)
label(fbar, text="🔍").pack(side=tk.LEFT, padx=(0, 6))
flt = field(tk.Entry, fbar)
flt.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
flt.bind("<KeyRelease>", render)
row = tk.Frame(chg_frame, bg=BG)
row.pack(fill=tk.X, padx=12, pady=(8, 4))
btn(row, "Tout", lambda: setstate(tree.get_children(), True), padx=10, pady=2).pack(side=tk.LEFT)
btn(row, "Rien", lambda: setstate(tree.get_children(), False), padx=10, pady=2).pack(side=tk.LEFT, padx=4)
btn(row, "Inverser", invert, padx=10, pady=2).pack(side=tk.LEFT)
label(row, textvariable=count_lbl, fg=MUTED).pack(side=tk.RIGHT)
box, tree = make_tree(chg_frame, ("st", "path"), show="tree headings", selectmode="extended")
box.pack(fill=tk.BOTH, expand=True, padx=12)
tree.heading("#0", text="", image=IMG["off"], command=toggle_all)  # la colonne #0 porte la case à cocher
tree.heading("st", text="")
tree.heading("path", text="Fichier", anchor="w")
tree.column("#0", width=46, stretch=False, anchor="center")
tree.column("st", width=34, stretch=False, anchor="center")
tree.column("path", anchor="w")
tree.tag_configure("new", foreground="#56d364")
tree.tag_configure("del", foreground=RED)
tree.tag_configure("ren", foreground=AMBER)
tree.bind("<Button-1>", press)
tree.bind("<Shift-Button-1>", press)
tree.bind("<Double-Button-1>", dbl)
tree.bind("<B1-Motion>", drag)
tree.bind("<ButtonRelease-1>", release)
tree.bind("<space>", key_toggle)
tree.bind("<<TreeviewSelect>>", on_tree_select)
tree.bind("<Control-a>", lambda e: (setstate(tree.get_children(), True), "break")[1])
for k in ("Shift_L", "Shift_R"):
    root.bind_all(f"<KeyPress-{k}>", lambda e: held.update(shift=True))
    root.bind_all(f"<KeyRelease-{k}>", lambda e: held.update(shift=False))
label(chg_frame, text="Case : clic · glisser · Maj+clic (plage) · Espace · Ctrl+A   |   ligne : aperçu · double-clic = cocher",
      fg=MUTED, font=("Segoe UI", 8)).pack(fill=tk.X, padx=14, pady=(3, 6))
form = tk.Frame(chg_frame, bg=BG)
form.pack(fill=tk.X, padx=12)
trow = tk.Frame(form, bg=BG)
trow.pack(fill=tk.X)
label(trow, text="Titre", fg=MUTED).pack(side=tk.LEFT)
ai_btn = btn(trow, "✨ Générer (IA)", ai, PURPLE, "white", FONT_B, padx=10, pady=2)
ai_btn.pack(side=tk.RIGHT)
summary = field(tk.Entry, form)
summary.pack(fill=tk.X, pady=(3, 8), ipady=6)
label(form, text="Description", fg=MUTED).pack(fill=tk.X)
desc = field(tk.Text, form, height=4, wrap=tk.WORD, padx=6, pady=6)
desc.pack(fill=tk.X, pady=(3, 8))
crow = tk.Frame(form, bg=BG)
crow.pack(fill=tk.X)
commit_btn = btn(crow, "", lambda: commit(False), BLUE, "white", FONT_B, pady=8)
commit_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
push_btn = btn(crow, "Commit & Push", lambda: commit(True), GREEN, "white", FONT_B, padx=14, pady=8)
push_btn.pack(side=tk.LEFT, padx=(6, 0))

# --- onglet Historique
hist_frame = tk.Frame(left, bg=BG)
fbox, ftree = make_tree(hist_frame, ("st", "path"), height=7, selectmode="browse")
fbox.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(2, 8))
label(hist_frame, text="Fichiers du commit", fg=MUTED).pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(6, 0))
hbox, htree = make_tree(hist_frame, ("subject", "author", "date"), selectmode="browse")
hbox.pack(fill=tk.BOTH, expand=True, padx=12)
htree.heading("subject", text="Commit", anchor="w")
htree.heading("author", text="Auteur", anchor="w")
htree.heading("date", text="Date", anchor="w")
htree.column("subject", anchor="w", width=220)
htree.column("author", anchor="w", width=90, stretch=False)
htree.column("date", anchor="w", width=100, stretch=False)
ftree.heading("st", text="")
ftree.heading("path", text="Fichier", anchor="w")
ftree.column("st", width=34, stretch=False, anchor="center")
ftree.column("path", anchor="w")
htree.bind("<<TreeviewSelect>>", on_commit)
ftree.bind("<<TreeviewSelect>>", on_hfile)

# --- volet droit : aperçu du diff
dhead = tk.Frame(right, bg=TOP, highlightthickness=1, highlightbackground=BORDER)
dhead.pack(fill=tk.X)
label(dhead, textvariable=diff_title, bg=TOP, font=FONT_B, padx=16, pady=12).pack(side=tk.LEFT)
label(dhead, textvariable=diff_del, bg=TOP, fg=RED, font=("Consolas", 10, "bold")).pack(side=tk.RIGHT, padx=(0, 16))
label(dhead, textvariable=diff_add, bg=TOP, fg="#56d364", font=("Consolas", 10, "bold")).pack(side=tk.RIGHT, padx=6)
dframe = tk.Frame(right, bg=PANEL)
dframe.pack(fill=tk.BOTH, expand=True)
d = tk.Text(dframe, wrap="none", bg=PANEL, fg=FG, font=MONO, relief=tk.FLAT, bd=0, highlightthickness=0,
            state=tk.DISABLED, cursor="arrow", padx=0, pady=6, insertbackground=PANEL, selectbackground=SEL)
dy = ttk.Scrollbar(dframe, orient="vertical", command=d.yview)
dx = ttk.Scrollbar(dframe, orient="horizontal", command=d.xview)
d.configure(yscrollcommand=dy.set, xscrollcommand=dx.set)
d.grid(row=0, column=0, sticky="nsew")
dy.grid(row=0, column=1, sticky="ns")
dx.grid(row=1, column=0, sticky="ew")
dframe.rowconfigure(0, weight=1)
dframe.columnconfigure(0, weight=1)
d.tag_configure("add", background="#0f2e1b")
d.tag_configure("del", background="#3b1219")
d.tag_configure("hunk", background="#12233a", foreground="#79b8ff")
d.tag_configure("meta", foreground=MUTED)
d.tag_configure("empty", foreground=MUTED, font=("Segoe UI", 12), lmargin1=70, spacing1=120)
for _t, _c in (("kw", "#ff7b72"), ("str", "#a5d6ff"), ("num", "#79c0ff"), ("typ", "#ffa657"), ("ann", "#d2a8ff"),
               ("tag", "#7ee787"), ("attr", "#79c0ff")):  # couleurs façon GitHub dark
    d.tag_configure(_t, foreground=_c)
d.tag_configure("com", foreground="#8b949e", font=("Consolas", 10, "italic"))
d.tag_configure("ln", foreground="#6e7681")  # créé en dernier : sa couleur l'emporte sur celle de add/del
btns = [ai_btn, commit_btn, push_btn, tb_fetch, tb_pull, tb_push]

try:
    with open(CONF, encoding="utf-8") as fh:
        repos = [p for p in json.load(fh) if os.path.isdir(p)]  # oublie les dossiers disparus
except (OSError, ValueError, TypeError):
    repos = []
combo["values"] = names()
start = sys.argv[1] if len(sys.argv) > 1 else (repos[0] if repos else "")
set_repo(start) if start else load()


def prewarm():
    try:
        spawn()
    except OSError:
        pass  # claude introuvable : l'erreur sera affichée à la première génération


threading.Thread(target=prewarm, daemon=True).start()
root.mainloop()
