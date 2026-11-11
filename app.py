import base64, datetime, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, webbrowser, tkinter as tk
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
HIST = {"all": []}  # commits chargés (hash, titre, auteur, date) ; la liste affichée en est un filtre


def run(cmd, env=None, **kw):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **profile_env(), **(env or {})}  # échoue au lieu de bloquer sur un prompt
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       stdin=subprocess.DEVNULL, creationflags=NOWIN, **kw)  # stdin explicite : requis dans un exe sans console
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} exit {r.returncode}")
    return r.stdout


NET = ("push", "pull", "fetch")


def git(*a, r=None):  # r : un autre dépôt que le courant (merge requests multi-dépôts)
    target = r or repo
    return run(["git", "-C", target, *a], env=git_auth_env(target) if a and a[0] in NET else None)


# ---------- IA : un processus claude déjà démarré (l'init de ~8 s se fait pendant que tu coches) ----------
SYS = "You write git commit messages in plain prose. Output ONLY the message: no code fences, no preamble."


def find_tool(name):
    # Lancée depuis le Finder / le Dock, l'app n'a pas le PATH du terminal : on cherche aux emplacements usuels.
    home = os.path.expanduser("~")
    extra = os.pathsep.join([os.path.join(home, ".local", "bin"), os.path.join(home, ".claude", "local"),
                             "/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".npm-global", "bin")])
    return shutil.which(name) or shutil.which(name, path=extra)


CLAUDE = [find_tool("claude") or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
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
        msg = res.get("result") or "erreur claude"
        if "authenticate" in msg or "login" in msg.lower():
            msg += "\n\nConnecte-toi : lance `claude` dans un terminal, puis tape /login."
        raise RuntimeError(msg)
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


def set_title(t):  # chemin : dossier estompé, nom du fichier en clair (comme GitHub Desktop)
    head, _, name = t.rpartition("/")
    diff_dir.set(head + "/" if head else "")
    diff_name.set(name)


def show_diff(text, title):
    set_title(title or "Sélectionne un fichier pour voir ses changements")
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
    HIST["all"] = []
    clist.clear()
    flist.clear()
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
        bcombo["values"] = branches_of()
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
        HIST["all"] = [tuple(l.split("\x1f")) for l in git("log", "-300", "--pretty=format:%h%x1f%s%x1f%an%x1f%ar").splitlines()]
    except RuntimeError:  # aucun commit
        HIST["all"] = []
    fill_commits()


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
def fill_commits(*_):  # liste filtrée par le champ de recherche (titre, auteur ou hash)
    q = hflt.get().strip().lower()
    clist.clear()
    for h, subject, author, when_ in HIST["all"]:
        if q in f"{subject} {author} {h}".lower():
            clist.add(h, commit_row(subject, author, when_))
    if not clist.ids:
        flist.clear()
        h_title.set("")


def on_commit(h):
    flist.clear()
    try:
        info = git("show", "-s", "--format=%an%x1f%H%x1f%s%x1f%b", h).rstrip("\n").split("\x1f", 3)
        names = git("show", "--first-parent", "-m", "--name-status", "--pretty=format:", "-M", h)
        nums = git("show", "--first-parent", "-m", "--numstat", "--pretty=format:", "-M", h)
    except RuntimeError as e:
        return status.set(str(e))
    author, full, subject, body = (info + ["", "", "", ""])[:4]
    h_title.set(subject)
    h_author.set(author)
    h_sha.set(h)
    HIST["full"] = full
    h_avatar.config(image=avatar(author))
    h_body.config(state=tk.NORMAL)
    h_body.delete("1.0", tk.END)
    h_body.insert(tk.END, body.strip())
    h_body.config(state=tk.DISABLED, height=min(6, max(1, len(body.strip().splitlines()))))
    h_body.pack_forget()
    if body.strip():
        h_body.pack(fill=tk.X, padx=16, pady=(0, 6), after=h_titlebar)
    add = dele = 0
    for line in nums.splitlines():
        p = line.split("\t")
        if len(p) > 2:
            add += int(p[0]) if p[0].isdigit() else 0
            dele += int(p[1]) if p[1].isdigit() else 0
    h_add.set(f"+{add}")
    h_del.set(f"−{dele}")
    for line in names.splitlines():
        p = line.split("\t")
        if len(p) > 1 and p[-1] not in flist.ids:
            flist.add(p[-1], file_row(p[0][0], p[-1]))
    n = len(flist.ids)
    files_lbl.set(f"{n} fichier{'s' if n > 1 else ''} modifié{'s' if n > 1 else ''}")
    if flist.ids:
        flist.set(0)
    else:
        show_diff("", f"{h} · aucun fichier")


def on_hfile(path):
    hs = clist.selection()
    if path and hs:
        try:
            out = git("show", "--first-parent", "-m", "-U3", "--no-color", "--pretty=format:", "-M", hs[0], "--", path)
        except RuntimeError as e:
            return status.set(str(e))
        show_diff(out, path)


def tab(which):
    frames = {"chg": chg_frame, "hist": hist_frame, "pipe": pipe_frame, "mr": mr_frame}
    labels_ = {"chg": tab_chg, "hist": tab_hist, "pipe": tab_pipe, "mr": tab_mr}
    for k, f in frames.items():
        f.pack_forget()
        labels_[k].config(fg=FG if k == which else MUTED)
        tab_bars[k].config(bg=BLUE if k == which else BG)
    frames[which].pack(fill=tk.BOTH, expand=True)
    if any(str(w) == str(fpanel) for w in rbody.panes()):  # colonne des fichiers et en-tête du commit : seulement dans Historique
        rbody.forget(fpanel)
    hhead.pack_forget()
    if which == "hist":
        hhead.pack(fill=tk.X, before=rbody)
        rbody.add(fpanel, before=dwrap, width=300, minsize=180)
    if which == "chg":
        sel = tree.selection()
        last["k"] = None
        preview(sel[0]) if len(sel) == 1 else show_diff("", "")
    elif which == "hist":
        if clist.selection():
            on_hfile(flist.selection()[0]) if flist.selection() else show_diff("", "")
        elif clist.ids:
            clist.set(0)  # le dernier commit s'ouvre tout seul
        else:
            show_diff("", "")
    elif which == "mr":
        if MRS["repo"] != repo:
            refresh_mrs()
        elif mtree.selection():
            on_mr(None)
    elif PIPE["repo"] != repo:  # première ouverture, ou autre dépôt depuis la dernière fois
        refresh_pipes()
    elif jtree.selection():
        on_job(None)
    else:
        show_log("Choisis un pipeline puis un job.", "Pipelines", note=True)


# ---------- tâches de fond ----------
def busy(var, label):  # « label… 1.3 s » rafraîchi pendant la tâche ; done(fn) fige « label · 1.3 s », lance fn, garde la durée si fn change le texte
    t0, live = time.monotonic(), [True]

    def tick():
        if live[0]:
            var.set(f"{label}… {time.monotonic() - t0:.1f} s")
            root.after(100, tick)
    tick()

    def done(fn):
        live[0] = False
        d = f"{time.monotonic() - t0:.1f} s"
        base = f"{label} · {d}"
        var.set(base)
        fn()
        if var.get() != base:
            var.set(f"{var.get()} · {d}")
    return done


def bg(job, then, label=None):  # label : nom de l'action ; à défaut, le texte « … » déjà posé dans la barre d'état
    for b in btns:
        b.config(state=tk.DISABLED)
    pb.place(relwidth=1, relheight=1)
    pb.start(12)
    s = status.get()
    done = busy(status, label or (s[:-1] if s.endswith("…") else "Chargement"))

    def t():
        try:
            r, err = job(), None
        except Exception as e:
            r, err = None, str(e)
        root.after(0, lambda: done(lambda: fin(then, r, err)))
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
        bg(lambda: git("checkout", name), lambda _: (load(), status.set(f"Branche : {name}")), f"Checkout {name}")


def new_branch():
    name = (simpledialog.askstring("Nouvelle branche", f"Nom (créée depuis {branch}) :", parent=root) or "").strip()
    if name:
        bg(lambda: git("checkout", "-b", name), lambda _: (load(), status.set(f"Branche créée : {name}")), f"Création de {name}")


def sync(label, *args):
    if repo:
        status.set(label + "…")
        bg(lambda: git(*args), lambda _: (load(), status.set(label + " OK")))


# ---------- merge requests ----------
SETTINGS = os.path.join(CONF_DIR, "settings.json")
PROD, DEV = ("master", "main"), "develop"  # cible "prod" → option : créer aussi la MR vers develop


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump({**load_settings(), **kw}, fh, indent=1)
        os.chmod(SETTINGS, 0o600)  # peut contenir des tokens
    except OSError:
        pass


def branches_of(r=None):  # locales puis distantes (origin/HEAD, sans slash, écartée)
    loc = git("for-each-ref", "--format=%(refname:short)", "refs/heads", r=r).split()
    rem = [x.split("/", 1)[1] for x in git("for-each-ref", "--format=%(refname:short)", "refs/remotes", r=r).split() if "/" in x]
    return loc + [x for x in rem if x not in loc]


def has_ref(ref, r=None):
    try:
        git("rev-parse", "--verify", "--quiet", ref, r=r)
        return True
    except RuntimeError:
        return False


def has_branch(name, r=None):  # en local ou sur origin
    return has_ref(f"refs/heads/{name}", r) or has_ref(f"refs/remotes/origin/{name}", r)


def ref_of(name, remote_first=False, r=None):  # la branche locale, sinon sa copie distante
    order = (f"refs/remotes/origin/{name}", f"refs/heads/{name}") if remote_first else (f"refs/heads/{name}", f"refs/remotes/origin/{name}")
    return next((x for x in order if has_ref(x, r)), order[0])


def to_merge(src, tgt, r=None):  # nombre de commits que la MR apporterait (cible = sa copie distante, c'est elle que GitLab compare)
    try:
        return int(git("rev-list", "--count", f"{ref_of(tgt, True, r)}..{ref_of(src, False, r)}", r=r))
    except (RuntimeError, ValueError):
        return None


def remote_web(r=None):  # (hôte, chemin) du remote origin, en https comme en ssh
    url = git("remote", "get-url", "origin", r=r).strip()
    m = re.match(r"^(?:[\w.+-]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", url)
    if not m:
        raise RuntimeError(f"Remote illisible : {url}")
    return m.group(1), m.group(2)


def mr_url(host, path, src, tgt, title, body):  # page de création pré-remplie (GitHub ou GitLab)
    if host == "github.com":
        q = urllib.parse.urlencode({"expand": 1, "title": title, "body": body})
        return f"https://github.com/{path}/compare/{urllib.parse.quote(tgt, safe='')}...{urllib.parse.quote(src, safe='')}?{q}"
    q = urllib.parse.urlencode({"merge_request[source_branch]": src, "merge_request[target_branch]": tgt,
                                "merge_request[title]": title, "merge_request[description]": body})
    return f"https://{host}/{path}/-/merge_requests/new?{q}"


PROFILES = {"list": [], "active": None}  # utilisateurs : identité git + tokens par hôte ; un seul est actif


def load_profiles():
    st = load_settings()
    lst = st.get("profiles") or [{"id": "p1", "name": "Moi", "git_name": "", "email": "", "tokens": st.get("tokens", {})}]  # reprend les tokens d'avant
    PROFILES["list"] = lst
    PROFILES["active"] = st.get("active") if any(x["id"] == st.get("active") for x in lst) else lst[0]["id"]


def save_profiles():
    save_settings(profiles=PROFILES["list"], active=PROFILES["active"], tokens={})


def me():
    return next((x for x in PROFILES["list"] if x["id"] == PROFILES["active"]), None) or {"name": "", "tokens": {}}


def profile_env():  # auteur et committer des commits : l'utilisateur actif (vide = config git du dépôt)
    p, env = me(), {}
    if p.get("git_name"):
        env.update(GIT_AUTHOR_NAME=p["git_name"], GIT_COMMITTER_NAME=p["git_name"])
    if p.get("email"):
        env.update(GIT_AUTHOR_EMAIL=p["email"], GIT_COMMITTER_EMAIL=p["email"])
    return env


def gitlab_token(host):
    return me().get("tokens", {}).get(host)


def save_token(host, token):
    tokens = me().setdefault("tokens", {})
    if token:
        tokens[host] = token
    else:
        tokens.pop(host, None)
    save_profiles()


def git_auth_env(target):  # push/pull/fetch en https avec le token de l'utilisateur actif (via l'environnement, pas les arguments)
    try:
        host = remote_web(target)[0]
    except RuntimeError:
        return None
    tok = gitlab_token(host)
    if not tok:
        return None
    basic = base64.b64encode(f"{'x-access-token' if host == 'github.com' else 'oauth2'}:{tok}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": f"http.https://{host}/.extraheader", "GIT_CONFIG_VALUE_1": f"Authorization: Basic {basic}"}


def cli_env(host):  # gh / glab agissent en tant que l'utilisateur actif
    tok = gitlab_token(host) if host else None
    if not tok:
        return None
    return {"GH_TOKEN": tok} if host == "github.com" else {"GITLAB_TOKEN": tok, "GITLAB_HOST": host}


def ci_env():
    try:
        return cli_env(remote_web()[0])
    except RuntimeError:
        return None


def ask_token(host):  # à appeler depuis la fenêtre principale ; le token est saisi dans l'app, jamais affiché
    scope = "repo" if host == "github.com" else "api"
    tok = simpledialog.askstring(f"Token — {me()['name']}", f"Token d'accès personnel de {me()['name']} pour {host} (scope {scope}).\n"
                                 "Il est gardé dans settings.json, lisible par toi seul.", show="•", parent=root)
    if tok and tok.strip():
        save_token(host, tok.strip())
        return True
    return False


def curl(url, token, data=()):  # (code HTTP, corps) ; le token passe par stdin : pas dans les arguments, visibles par `ps`
    esc = lambda v: v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    cfg = f'header = "PRIVATE-TOKEN: {esc(token)}"\n' + "".join(f'data-urlencode = "{k}={esc(v)}"\n' for k, v in data)
    r = subprocess.run([find_tool("curl") or "curl", "-sS", "-m", "60", "-K", "-", "-w", "\n%{http_code}", url], input=cfg,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    if r.returncode:
        raise RuntimeError((r.stderr or "curl a échoué").strip())
    body, _, code = r.stdout.rpartition("\n")
    return int(code or 0), body


def gl_call(host, path, endpoint, data=()):  # API GitLab avec le token enregistré ; ":id" = le projet
    token = gitlab_token(host)
    if not token:
        raise RuntimeError(f"Pas de token GitLab pour {host} (bouton « Token… » ou à la création de la MR).")
    code, body = curl(f"https://{host}/api/v4/" + endpoint.replace(":id", urllib.parse.quote(path, safe="")), token, data)
    if code == 401:
        save_token(host, None)
        raise RuntimeError(f"Token refusé par {host} (401) : il a été oublié, saisis-en un nouveau.")
    if code >= 400:
        try:
            msg = json.loads(body).get("message") or body
        except (ValueError, AttributeError):
            msg = body
        raise RuntimeError(f"GitLab {code} : {msg if isinstance(msg, str) else '; '.join(map(str, msg)) if isinstance(msg, list) else msg}"[:300])
    return body


def create_mr(src, tgt, title, body, r=None):  # création directe : gh (GitHub), glab ou API GitLab (token) ; jamais le navigateur
    host, path = remote_web(r)
    if host == "github.com":
        cli = find_tool("gh")
        if not cli:
            raise RuntimeError("Installe gh (brew install gh) puis gh auth login.")
        out = run([cli, "pr", "create", "--head", src, "--base", tgt, "--title", title, "--body", body], cwd=r or repo, env=cli_env(host))
    elif find_tool("glab"):
        out = run([find_tool("glab"), "mr", "create", "--source-branch", src, "--target-branch", tgt, "--title", title,
                   "--description", body, "--yes"], cwd=r or repo, env=cli_env(host))
    else:
        out = gl_call(host, path, "projects/:id/merge_requests", [("source_branch", src), ("target_branch", tgt), ("title", title), ("description", body)])
        try:
            return json.loads(out)["web_url"]
        except (ValueError, KeyError):
            return out.strip()[:200]
    urls = re.findall(r"https?://\S+", out)
    return urls[-1] if urls else out.strip()


def merge_dialog():  # une source → une cible, créées dans tous les dépôts cochés
    if not repo:
        return
    labels_of = dict(zip(repos, names()))
    all_b = []
    for r_ in [repo] + [x for x in repos if x != repo]:  # branches du dépôt courant d'abord, puis celles des autres
        try:
            all_b += [b for b in branches_of(r_) if b not in all_b]
        except RuntimeError:
            pass
    if len(all_b) < 2:
        return messagebox.showwarning("Merge request", "Il faut au moins deux branches.")
    prefs = load_settings()
    dlg = tk.Toplevel(root, bg=BG)
    dlg.title("Créer des merge requests")
    dlg.transient(root)
    dlg.resizable(False, False)
    box_ = tk.Frame(dlg, bg=BG)
    box_.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)

    src_v = tk.StringVar(value=branch if branch in all_b else all_b[0])
    default_tgt = next((b for b in (DEV, *PROD) if b in all_b and b != src_v.get()), next(b for b in all_b if b != src_v.get()))
    tgt_v = tk.StringVar(value=default_tgt)
    both_v = tk.BooleanVar(value=bool(prefs.get("mr_both")))

    pick_row = tk.Frame(box_, bg=BG)
    pick_row.pack(fill=tk.X)
    src_box = BranchBox(pick_row, "Source — la branche à fusionner", all_b, src_v, lambda: refresh())
    tgt_box = BranchBox(pick_row, "Cible — celle qui reçoit", all_b, tgt_v, lambda: refresh())
    src_box.grid(row=0, column=0, sticky="nsew")
    tgt_box.grid(row=0, column=2, sticky="nsew")
    pick_row.columnconfigure((0, 2), weight=1)

    def swap():
        s, t = src_v.get(), tgt_v.get()
        src_v.set(t)
        tgt_v.set(s)
        src_box.fill()
        tgt_box.fill()
        refresh()
    btn(pick_row, "⇄", swap, padx=10, pady=3).grid(row=0, column=1, padx=10)

    label(box_, text="DÉPÔTS", fg=MUTED, font=SMALL_B).pack(fill=tk.X, pady=(12, 3))
    rbox = tk.Frame(box_, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    rbox.pack(fill=tk.X)
    rvars, rinfo = {}, {}
    for path_ in repos:  # le dépôt courant est coché ; les autres sont à cocher
        rvars[path_] = tk.BooleanVar(value=path_ == repo)
        row_ = tk.Frame(rbox, bg=PANEL)
        row_.pack(fill=tk.X, padx=8, pady=3)
        tk.Checkbutton(row_, text=labels_of[path_], variable=rvars[path_], command=lambda: refresh(), bg=PANEL, fg=FG, selectcolor=FIELD,
                       activebackground=PANEL, activeforeground=FG, font=FONT, anchor="w", highlightthickness=0, bd=0).pack(side=tk.LEFT)
        rinfo[path_] = tk.Label(row_, bg=PANEL, fg=MUTED, font=SMALL, anchor="e")
        rinfo[path_].pack(side=tk.RIGHT)
    allrow = tk.Frame(box_, bg=BG)
    allrow.pack(fill=tk.X, pady=(6, 0))
    btn(allrow, "Tous", lambda: [v.set(True) for v in rvars.values()] and refresh(), padx=10, pady=2).pack(side=tk.LEFT)
    btn(allrow, "Aucun", lambda: [v.set(False) for v in rvars.values()] and refresh(), padx=10, pady=2).pack(side=tk.LEFT, padx=4)
    info = tk.StringVar()
    info_lbl = label(allrow, textvariable=info, fg=MUTED)
    info_lbl.pack(side=tk.RIGHT)

    label(box_, text="Titre", fg=MUTED).pack(fill=tk.X, pady=(10, 0))
    ttl = field(tk.Entry, box_)
    ttl.pack(fill=tk.X, pady=(3, 8), ipady=6)
    label(box_, text="Description (optionnel)", fg=MUTED).pack(fill=tk.X)
    body = field(tk.Text, box_, height=4, wrap=tk.WORD, padx=6, pady=6)
    body.pack(fill=tk.X, pady=(3, 8))
    both_cb = tk.Checkbutton(box_, variable=both_v, bg=BG, fg=FG, selectcolor=FIELD, activebackground=BG, activeforeground=FG,
                             disabledforeground=MUTED, font=FONT, anchor="w", highlightthickness=0, bd=0, command=lambda: refresh(),
                             text=f"Cible master/main : créer aussi une MR vers {DEV}")
    both_cb.pack(fill=tk.X, pady=(0, 10))

    brow = tk.Frame(box_, bg=BG)
    brow.pack(fill=tk.X)
    btn(brow, "Annuler", dlg.destroy, padx=14, pady=7).pack(side=tk.RIGHT, padx=(6, 0))
    ok = btn(brow, "Créer les MR", lambda: submit(), PURPLE, "white", FONT_B, padx=16, pady=7)
    ok.pack(side=tk.RIGHT)

    auto = {"title": ""}
    plans = {}  # dépôt → (faisable, texte, cible develop en plus)

    def plan(path_, s, t):
        if not has_branch(s, path_):
            return False, f"« {s} » absente", None
        if not has_branch(t, path_):
            return False, f"« {t} » absente", None
        n = to_merge(s, t, path_)
        if n == 0:
            return False, "rien à fusionner", None
        extra = DEV if t in PROD and s != DEV and has_branch(DEV, path_) else None
        return True, (f"{n} commit{'s' if n > 1 else ''}" if n else "prêt") + (f" + {DEV}" if extra and both_v.get() else ""), extra

    def refresh():
        s, t = src_v.get(), tgt_v.get()
        same = s == t
        for path_ in repos:
            plans[path_] = (False, "source = cible", None) if same else plan(path_, s, t)
            good, text, _ = plans[path_]
            rinfo[path_].config(text=("✔ " if good else "⚠ ") + text, fg="#56d364" if good else AMBER)
        chosen = [p_ for p_ in repos if rvars[p_].get() and plans[p_][0]]
        skipped = sum(1 for p_ in repos if rvars[p_].get() and not plans[p_][0])
        info.set("⚠ Source et cible identiques" if same else f"{len(chosen)} MR à créer" + (f" · {skipped} ignoré(s)" if skipped else ""))
        info_lbl.config(fg=AMBER if same or skipped else MUTED)
        ok.config(state=tk.NORMAL if chosen else tk.DISABLED)
        both_cb.config(state=tk.NORMAL if t in PROD and s != DEV and not same else tk.DISABLED)
        try:  # titre proposé : dernier commit de la source ; remplacé tant que tu n'y as pas touché
            subject = git("log", "-1", "--pretty=%s", ref_of(s)).strip()
        except RuntimeError:
            subject = s
        if ttl.get().strip() in ("", auto["title"]):
            ttl.delete(0, tk.END)
            ttl.insert(0, subject)
            auto["title"] = subject

    def submit():
        s, t, title, desc_ = src_v.get(), tgt_v.get(), ttl.get().strip(), body.get("1.0", tk.END).strip()
        if not title:
            return messagebox.showwarning("Merge request", "Il faut un titre.", parent=dlg)
        both = bool(both_v.get())
        save_settings(mr_both=both)
        todo = [(p_, plans[p_][2] if both else None) for p_ in repos if rvars[p_].get() and plans[p_][0]]
        skipped = [(p_, plans[p_][1]) for p_ in repos if rvars[p_].get() and not plans[p_][0]]
        dlg.destroy()
        if not find_tool("glab"):  # pas de glab : API GitLab, il faut un token par hôte
            hosts = set()
            for path_, _ in todo:
                try:
                    hosts.add(remote_web(path_)[0])
                except RuntimeError:
                    pass
            for host in sorted(hosts - {"github.com"}):
                if not gitlab_token(host):
                    ask_token(host)
        status.set("Création des MR…")

        def job():
            out, failed = [], False
            for path_, extra in todo:  # une erreur dans un dépôt n'arrête pas les autres
                lines = []
                try:
                    if has_ref(f"refs/heads/{s}", path_):
                        git("push", "-u", "origin", s, r=path_)  # la MR a besoin de la branche source sur le distant
                    for tg in [t] + ([extra] if extra else []):
                        lines.append(f"  {s} → {tg} : {create_mr(s, tg, title, desc_, path_)}")
                except RuntimeError as e:
                    lines.append(f"  ÉCHEC — {e}")
                    failed = True
                out.append(labels_of[path_] + "\n" + "\n".join(lines))
            out += [f"{labels_of[p_]}\n  ignoré : {why}" for p_, why in skipped]
            return "\n\n".join(out), failed

        def then(r):
            text, failed = r
            status.set("MR : des erreurs" if failed else "MR créées")
            (messagebox.showwarning if failed else messagebox.showinfo)("Merge requests", text)
        bg(job, then)

    refresh()
    try:
        dlg.wait_visibility()
        dlg.grab_set()
    except tk.TclError:
        pass


# ---------- pipelines et jobs (gh pour GitHub, glab pour GitLab) ----------
PIPE = {"repo": None, "tok": 0, "rows": [], "jobs": []}  # tok : ignore les réponses d'une requête périmée
LOG_LINES = 600
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ERRLINE = re.compile(r"\b(error|errors|failed|failure|fatal|exception)\b", re.I)
GH_DONE = {"success": "success", "neutral": "success", "failure": "failed", "timed_out": "failed", "startup_failure": "failed",
           "cancelled": "canceled", "stale": "canceled", "skipped": "skipped", "action_required": "pending"}
STATE = {  # statut → (symbole, couleur de ligne, libellé)
    "success": ("✔", "ok", "réussi"), "failed": ("✖", "bad", "échec"), "running": ("●", "run", "en cours"),
    "pending": ("◷", "wait", "en attente"), "created": ("◷", "wait", "créé"), "preparing": ("◷", "wait", "préparation"),
    "waiting_for_resource": ("◷", "wait", "en attente"), "scheduled": ("◷", "wait", "planifié"), "manual": ("▶", "wait", "manuel"),
    "canceled": ("⊘", "skip", "annulé"), "skipped": ("↷", "skip", "ignoré"),
}


def gh_status(r):  # GitHub : statut + conclusion → vocabulaire GitLab
    if r.get("status") == "completed":
        return GH_DONE.get(r.get("conclusion"), "failed")
    return "running" if r.get("status") == "in_progress" else "pending"


def when(iso):
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ago(iso):
    t = when(iso)
    if not t:
        return ""
    s = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
    return "à l'instant" if s < 60 else f"il y a {s // 60} min" if s < 3600 else f"il y a {s // 3600} h" if s < 86400 else f"il y a {s // 86400} j"


def secs(start, end):  # durée GitHub ; "0001-01-01…" = pas démarré
    a, b = when(start), when(end)
    if not a or a.year < 2000:
        return None
    return ((b if b and b.year >= 2000 else datetime.datetime.now(datetime.timezone.utc)) - a).total_seconds()


def dur(s):
    if s is None:
        return ""
    s = int(s)
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def ci_tool():  # (genre, outil) ; GitLab sans glab → outil None : API avec token
    host, path = remote_web()
    if host == "github.com":
        cli = find_tool("gh")
        if not cli:
            raise RuntimeError("Pour lister les pipelines ici, installe `gh` (brew install gh) puis gh auth login.")
        return "github", cli
    return "gitlab", find_tool("glab")


def gl_get(cli, endpoint):  # glab si présent, sinon l'API avec le token enregistré
    if cli:
        return run([cli, "api", endpoint], cwd=repo, env=ci_env())
    host, path = remote_web()
    return gl_call(host, path, endpoint)


def fetch_pipelines():
    kind, cli = ci_tool()
    if kind == "github":
        rows = json.loads(run([cli, "run", "list", "-L", "30", "--json",
                               "databaseId,status,conclusion,headBranch,displayTitle,workflowName,createdAt,url"], cwd=repo, env=ci_env()))
        return [dict(id=r["databaseId"], status=gh_status(r), ref=r["headBranch"], info=f'{r["workflowName"]} · {r["displayTitle"]}',
                     when=r["createdAt"], url=r["url"]) for r in rows]
    rows = json.loads(gl_get(cli, "projects/:id/pipelines?per_page=30"))
    return [dict(id=r["id"], status=r["status"], ref=r["ref"], info=f'#{r["id"]} · {r["sha"][:8]} · {r.get("source", "")}',
                 when=r["created_at"], url=r["web_url"]) for r in rows]


def fetch_jobs(p):
    kind, cli = ci_tool()
    if kind == "github":
        data = json.loads(run([cli, "run", "view", str(p["id"]), "--json", "jobs"], cwd=repo, env=ci_env()))
        return [dict(id=j["databaseId"], name=j["name"], stage="", status=gh_status(j),
                     dur=secs(j.get("startedAt"), j.get("completedAt")), url=j["url"]) for j in data["jobs"]]
    rows = json.loads(gl_get(cli, f"projects/:id/pipelines/{p['id']}/jobs?per_page=100"))
    return [dict(id=j["id"], name=j["name"], stage=j.get("stage", ""), status=j["status"], dur=j.get("duration"), url=j["web_url"])
            for j in sorted(rows, key=lambda j: j["id"])]


def fetch_log(j):  # fin du log, sans codes couleur
    kind, cli = ci_tool()
    if kind == "github":
        out = run([cli, "run", "view", "--job", str(j["id"]), "--log"], cwd=repo, env=ci_env())
        out = "\n".join(re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?", "", l.split("\t", 2)[-1]) for l in out.splitlines())
    else:
        out = gl_get(cli, f"projects/:id/jobs/{j['id']}/trace")
    lines = ANSI.sub("", out).replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cut = len(lines) - LOG_LINES
    return (f"… {cut} lignes plus haut\n" if cut > 0 else "") + "\n".join(lines[-LOG_LINES:])


def bgq(job, then, fail, var=None, label="Chargement"):  # tâche de fond qui ne bloque pas les boutons : l'erreur va dans l'aperçu ; var : où afficher le temps écoulé
    done = busy(var or status, label)

    def t():
        try:
            r, err = job(), None
        except Exception as e:
            r, err = None, str(e)
        root.after(0, lambda: done(lambda: fail(err) if err else then(r)))
    threading.Thread(target=t, daemon=True).start()


def show_log(text, title, note=False):  # texte brut dans l'aperçu de droite ; lignes d'erreur surlignées
    set_title(title)
    diff_add.set("")
    diff_del.set("")
    d.config(state=tk.NORMAL)
    d.delete("1.0", tk.END)
    if note:
        d.insert(tk.END, text, "empty")
    else:
        for line in text.splitlines():
            d.insert(tk.END, line + "\n", "del" if ERRLINE.search(line) else ())
        d.see(tk.END)
    d.config(state=tk.DISABLED)


def refresh_pipes(*_):
    if not repo:
        return
    PIPE["tok"] += 1
    tok, PIPE["repo"] = PIPE["tok"], repo
    PIPE["rows"], PIPE["jobs"] = [], []
    ptree.delete(*ptree.get_children())
    jtree.delete(*jtree.get_children())
    pipe_info.set("Chargement…")

    def ok(rows):
        if tok != PIPE["tok"]:
            return
        PIPE["rows"] = rows
        for i, r in enumerate(rows):
            sym, tag, _ = STATE.get(r["status"], ("?", "wait", r["status"]))
            ptree.insert("", tk.END, iid=str(i), values=(sym, r["ref"], r["info"], ago(r["when"])), tags=(tag,))
        pipe_info.set(f"{len(rows)} pipeline(s)" if rows else "Aucun pipeline")
        if rows:
            ptree.selection_set("0")
        else:
            show_log("Aucun pipeline pour ce dépôt.", "Pipelines", note=True)

    def fail(err):
        if tok == PIPE["tok"]:
            pipe_info.set("Indisponible")
            show_log(err, "Pipelines", note=True)
    bgq(fetch_pipelines, ok, fail, pipe_info, "Chargement")


def on_pipe(_):
    sel = ptree.selection()
    if not sel or not PIPE["rows"]:
        return
    p, tok = PIPE["rows"][int(sel[0])], PIPE["tok"]
    jtree.delete(*jtree.get_children())
    PIPE["jobs"] = []
    show_log("Chargement des jobs…", f"Pipeline {p['id']}", note=True)

    def ok(jobs):
        if tok != PIPE["tok"] or ptree.selection() != sel:
            return
        PIPE["jobs"] = jobs
        for i, j in enumerate(jobs):
            sym, tag, _ = STATE.get(j["status"], ("?", "wait", j["status"]))
            jtree.insert("", tk.END, iid=str(i), values=(sym, j["name"], j["stage"], dur(j["dur"])), tags=(tag,))
        bad = next((str(i) for i, j in enumerate(jobs) if j["status"] == "failed"), None)  # le job en échec s'ouvre tout seul
        if bad is not None:
            jtree.selection_set(bad)
            jtree.see(bad)
        else:
            show_log("Choisis un job pour voir son log." if jobs else "Aucun job.", f"Pipeline {p['id']} · {p['ref']}", note=True)
    bgq(lambda: fetch_jobs(p), ok, lambda err: show_log(err, "Pipelines", note=True), label=f"Jobs du pipeline {p['id']}")


def on_job(_):
    sel = jtree.selection()
    if not sel or not PIPE["jobs"]:
        return
    j, tok = PIPE["jobs"][int(sel[0])], PIPE["tok"]
    title = f"{j['name']} · {STATE.get(j['status'], ('', '', j['status']))[2]}"
    show_log("Chargement du log…", title, note=True)
    bgq(lambda: fetch_log(j), lambda t: tok == PIPE["tok"] and jtree.selection() == sel and show_log(t or "(log vide)", title),
        lambda err: show_log(err, title, note=True), label=f"Log de {j['name']}")


def set_token():  # saisie (ou remplacement) du token GitLab de l'hôte du dépôt courant
    try:
        host = remote_web()[0]
    except RuntimeError as e:
        return status.set(str(e))
    if ask_token(host):
        (refresh_pipes if pipe_frame.winfo_ismapped() else refresh_mrs)()


def open_web(which=None):  # "p"/"j" : le pipeline / le job choisi ; sinon (bouton) job, pipeline, puis page des pipelines du site
    js, ps = jtree.selection(), ptree.selection()
    if which != "p" and js and PIPE["jobs"]:
        url = PIPE["jobs"][int(js[0])]["url"]
    elif which != "j" and ps and PIPE["rows"]:
        url = PIPE["rows"][int(ps[0])]["url"]
    else:
        try:
            host, path = remote_web()
        except RuntimeError as e:
            return status.set(str(e))
        url = f"https://github.com/{path}/actions" if host == "github.com" else f"https://{host}/{path}/-/pipelines"
    webbrowser.open(url)


# ---------- merge requests créées par l'utilisateur actif (gh pour GitHub, glab ou API + token pour GitLab) ----------
MRS = {"repo": None, "tok": 0, "rows": []}
MR_STATE = {"opened": ("●", "run", "ouverte"), "open": ("●", "run", "ouverte"), "merged": ("✔", "ok", "mergée"),
            "closed": ("⊘", "skip", "fermée"), "locked": ("◷", "wait", "verrouillée")}


def fetch_mrs():
    kind, cli = ci_tool()
    if kind == "github":
        rows = json.loads(run([cli, "pr", "list", "--author", "@me", "--state", "all", "-L", "50", "--json",
                               "number,title,state,headRefName,baseRefName,createdAt,url"], cwd=repo, env=ci_env()))
        return [dict(ref=f'#{r["number"]}', state=r["state"].lower(), title=r["title"], src=r["headRefName"], tgt=r["baseRefName"],
                     when=r["createdAt"], url=r["url"]) for r in rows]
    rows = json.loads(gl_get(cli, "projects/:id/merge_requests?scope=created_by_me&state=all&per_page=50"))
    return [dict(ref=f'!{r["iid"]}', state=r["state"], title=r["title"], src=r["source_branch"], tgt=r["target_branch"],
                 when=r["created_at"], url=r["web_url"]) for r in rows]


def refresh_mrs(*_):
    if not repo:
        return
    MRS["tok"] += 1
    tok, MRS["repo"] = MRS["tok"], repo
    MRS["rows"] = []
    mtree.delete(*mtree.get_children())
    mr_info.set("Chargement…")

    def ok(rows):
        if tok != MRS["tok"]:
            return
        MRS["rows"] = rows
        for i, r in enumerate(rows):
            sym, tag, _ = MR_STATE.get(r["state"], ("?", "wait", r["state"]))
            mtree.insert("", tk.END, iid=str(i), values=(sym, f'{r["ref"]} · {r["title"]}', f'{r["src"]} → {r["tgt"]}', ago(r["when"])), tags=(tag,))
        mr_info.set(f"{len(rows)} MR" if rows else "Aucune MR")
        if rows:
            mtree.selection_set("0")
        else:
            show_log("Aucune merge request créée par toi sur ce dépôt.", "Merge requests", note=True)

    def fail(err):
        if tok == MRS["tok"]:
            mr_info.set("Indisponible")
            show_log(err, "Merge requests", note=True)
    bgq(fetch_mrs, ok, fail, mr_info, "Chargement")


def on_mr(_):
    sel = mtree.selection()
    if not sel or not MRS["rows"]:
        return
    r = MRS["rows"][int(sel[0])]
    show_log(f'{r["title"]}\n\n{r["src"]} → {r["tgt"]}\nÉtat : {MR_STATE.get(r["state"], ("", "", r["state"]))[2]}\nCréée {ago(r["when"])}\n{r["url"]}',
             f'{r["ref"]} · {r["title"]}', note=True)


def open_mr(_=None):
    sel = mtree.selection()
    if sel and MRS["rows"]:
        webbrowser.open(MRS["rows"][int(sel[0])]["url"])


# ---------- utilisateurs (profils) ----------
def refresh_users():
    names_ = [x["name"] for x in PROFILES["list"]]
    ucombo["values"] = names_
    ucombo.current(next(i for i, x in enumerate(PROFILES["list"]) if x["id"] == PROFILES["active"]))
    uav.config(image=avatar(me()["name"]))
    p = me()
    ident = " ".join(x for x in (p.get("git_name"), f"<{p['email']}>" if p.get("email") else "") if x)
    as_lbl.set(f"Commit en tant que {ident}" if ident else "Commit avec la config git du dépôt")


def switch_user(_=None):
    PROFILES["active"] = PROFILES["list"][ucombo.current()]["id"]
    save_profiles()
    refresh_users()
    PIPE["repo"] = MRS["repo"] = None  # pipelines et MR se rechargent avec le token du nouvel utilisateur
    if pipe_frame.winfo_ismapped():
        refresh_pipes()
    elif mr_frame.winfo_ismapped():
        refresh_mrs()
    status.set(f"Utilisateur : {me()['name']}")


def git_identity():  # valeurs de départ d'un nouveau profil : la config git globale
    out = []
    for key in ("user.name", "user.email"):
        try:
            out.append(run(["git", "config", "--global", key]).strip())
        except (RuntimeError, OSError):
            out.append("")
    return out


def profile_dialog(edit):
    cur = me() if edit else None
    gname, gmail = (cur.get("git_name", ""), cur.get("email", "")) if cur else git_identity()
    tokens = dict(cur.get("tokens", {})) if cur else {}
    dlg = tk.Toplevel(root, bg=BG)
    dlg.title("Modifier l'utilisateur" if edit else "Nouvel utilisateur")
    dlg.transient(root)
    dlg.resizable(False, False)
    box_ = tk.Frame(dlg, bg=BG)
    box_.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
    ents = {}
    for key, cap, val in (("name", "Nom du profil (ce que tu vois dans la liste)", cur["name"] if cur else ""),
                          ("git_name", "Nom des commits (git user.name)", gname), ("email", "Email des commits (git user.email)", gmail)):
        label(box_, text=cap, fg=MUTED).pack(fill=tk.X)
        ents[key] = field(tk.Entry, box_, width=44)
        ents[key].insert(0, val)
        ents[key].pack(fill=tk.X, pady=(3, 8), ipady=5)
    label(box_, text="Tokens (hôte) — GitLab : scope api · GitHub : scope repo", fg=MUTED).pack(fill=tk.X)
    tbox = tk.Frame(box_, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    tbox.pack(fill=tk.X, pady=(3, 6))
    lb = tk.Listbox(tbox, height=4, activestyle="none", exportselection=False, bg=PANEL, fg=FG, font=FONT, relief=tk.FLAT, bd=0,
                    highlightthickness=0, selectbackground=SEL, selectforeground=FG)
    lb.pack(fill=tk.X)

    def fill_tokens():
        lb.delete(0, tk.END)
        lb.insert(tk.END, *[f"{h}   ••••••••" for h in tokens])

    def add_token():
        host = (simpledialog.askstring("Hôte", "Hôte du token (ex. gitlab.ibonia.com ou github.com) :", parent=dlg) or "").strip()
        tok = simpledialog.askstring("Token", f"Token pour {host} :", show="•", parent=dlg) if host else None
        if host and tok and tok.strip():
            tokens[host] = tok.strip()
            fill_tokens()

    def drop_token():
        if lb.curselection():
            tokens.pop(list(tokens)[lb.curselection()[0]], None)
            fill_tokens()
    fill_tokens()
    trow = tk.Frame(box_, bg=BG)
    trow.pack(fill=tk.X, pady=(0, 12))
    btn(trow, "Ajouter un token…", add_token, padx=10, pady=3).pack(side=tk.LEFT)
    btn(trow, "Retirer", drop_token, padx=10, pady=3).pack(side=tk.LEFT, padx=4)

    def save():
        name = ents["name"].get().strip()
        if not name:
            return messagebox.showwarning("Utilisateur", "Il faut un nom de profil.", parent=dlg)
        if any(x["name"] == name and x is not cur for x in PROFILES["list"]):
            return messagebox.showwarning("Utilisateur", "Ce nom existe déjà.", parent=dlg)
        data = {"name": name, "git_name": ents["git_name"].get().strip(), "email": ents["email"].get().strip(), "tokens": tokens}
        if cur:
            cur.update(data)
        else:
            PROFILES["list"].append({"id": f"p{int(datetime.datetime.now().timestamp() * 1000)}", **data})
            PROFILES["active"] = PROFILES["list"][-1]["id"]  # le nouvel utilisateur devient actif
        save_profiles()
        dlg.destroy()
        refresh_users()
        switch_user() if not edit else None
    brow = tk.Frame(box_, bg=BG)
    brow.pack(fill=tk.X)
    btn(brow, "Annuler", dlg.destroy, padx=14, pady=7).pack(side=tk.RIGHT, padx=(6, 0))
    btn(brow, "Enregistrer", save, BLUE, "white", FONT_B, padx=16, pady=7).pack(side=tk.RIGHT)
    try:
        dlg.wait_visibility()
        dlg.grab_set()
    except tk.TclError:
        pass


def delete_user():
    if len(PROFILES["list"]) < 2:
        return messagebox.showinfo("Utilisateur", "Il faut garder au moins un utilisateur.")
    if messagebox.askyesno("Supprimer l'utilisateur", f"Supprimer « {me()['name']} » et ses tokens ?"):
        PROFILES["list"].remove(me())
        PROFILES["active"] = PROFILES["list"][0]["id"]
        save_profiles()
        refresh_users()
        switch_user()


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
    if pipe_frame.winfo_ismapped():  # l'onglet Pipelines est ouvert : il suit le dépôt
        refresh_pipes()
    elif mr_frame.winfo_ismapped():
        refresh_mrs()


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
MAC = sys.platform == "darwin"  # polices du système, comme GitHub Desktop : San Francisco + Menlo sur Mac, Segoe UI + Consolas sur Windows
SANS, MONOF = (".AppleSystemUIFont", "Menlo") if MAC else ("Segoe UI", "Consolas")
SZ = 11 if MAC else 9
FONT, FONT_B, MONO = (SANS, SZ), (SANS, SZ, "bold"), (MONOF, SZ - 1 if MAC else SZ)
SMALL, SMALL_B, BIG = (SANS, SZ - 2), (SANS, SZ - 2, "bold"), (SANS, SZ + 2, "bold")
MONO_B, MONO_I = (MONOF, MONO[1], "bold"), (MONOF, MONO[1], "italic")


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
        self.down = False  # feedback immédiat au clic : le bouton s'éclaircit et le texte descend d'un pixel
        self.bind("<ButtonPress-1>", lambda e: self.press(True))
        self.bind("<ButtonRelease-1>", self.click)
        self.config(text=text)

    def press(self, v):
        self.down = v and self.on
        self.draw()

    def hover(self, v):
        self.hov = v
        self.draw()

    def click(self, e):
        self.press(False)
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
        self.create_polygon(pts, smooth=True, outline="", fill=lighten(lighten(self.color)) if self.down else lighten(self.color) if self.hov and self.on else self.color)
        self.create_text(w // 2, h // 2 + self.down, text=self.text, fill=self.fg if self.on else MUTED, font=self.fnt)


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


class BranchBox(tk.Frame):  # sélecteur de branche filtrable : valeur choisie, champ de recherche, liste
    def __init__(self, parent, caption, values, var, on_change):
        super().__init__(parent, bg=BG)
        self.values, self.var, self.cb, self.shown, self.q = values, var, on_change, [], tk.StringVar()
        label(self, text=caption.upper(), fg=MUTED, font=SMALL_B).pack(fill=tk.X)
        label(self, textvariable=var, font=FONT_B, pady=3).pack(fill=tk.X)
        entry = field(tk.Entry, self, textvariable=self.q)
        entry.pack(fill=tk.X, ipady=4)
        box = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.lb = tk.Listbox(box, height=6, width=30, activestyle="none", exportselection=False, bg=PANEL, fg=FG, font=FONT, relief=tk.FLAT,
                             bd=0, highlightthickness=0, selectbackground=SEL, selectforeground=FG)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.q.trace_add("write", lambda *_: self.fill())
        self.lb.bind("<<ListboxSelect>>", lambda e: self.choose(self.lb.curselection()))
        entry.bind("<Return>", lambda e: self.choose((0,)))  # Entrée : la première branche qui correspond
        entry.bind("<Down>", lambda e: (self.lb.focus_set(), self.lb.selection_set(0) if self.shown else None, "break")[-1])
        self.fill()

    def fill(self):
        q = self.q.get().strip().lower()
        self.shown = [v for v in self.values if q in v.lower()]
        self.lb.delete(0, tk.END)
        self.lb.insert(tk.END, *self.shown)
        if self.var.get() in self.shown:
            i = self.shown.index(self.var.get())
            self.lb.selection_set(i)
            self.lb.see(i)

    def choose(self, sel):
        if sel and sel[0] < len(self.shown):
            self.var.set(self.shown[sel[0]])
            self.cb()


AVATARS = {}


def avatar(name):  # pastille ronde de couleur (stable par auteur), à la place de la photo de GitHub Desktop
    if name not in AVATARS:
        col = ("#8957e5", "#2f81f7", "#2ea043", "#d29922", "#db61a2", "#f0883e", "#39c5cf", "#a371f7")[sum(map(ord, name)) % 8]
        im = tk.PhotoImage(width=18, height=18)
        for y in range(18):
            for x in range(18):
                if (x - 8.5) ** 2 + (y - 8.5) ** 2 <= 8.5 ** 2:
                    im.put(col, (x, y))
        AVATARS[name] = im
    return AVATARS[name]


class RowList(tk.Frame):  # liste à lignes libres (1 ou 2 lignes par entrée) : souris, flèches, entrée surlignée
    def __init__(self, parent, per=1, select=None, fit=False):
        super().__init__(parent, bg=PANEL)
        self.per, self.cb, self.fit, self.ids, self.builds, self.cur, self.w, self.job = per, select, fit, [], [], None, 0, None
        self.t = tk.Text(self, wrap="none", bg=PANEL, fg=FG, font=FONT, relief=tk.FLAT, bd=0, highlightthickness=0, cursor="arrow",
                         state=tk.DISABLED, padx=0, pady=0, exportselection=False, insertwidth=0, selectbackground=PANEL, selectforeground=FG)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.t.yview)
        self.t.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.t.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.t.tag_configure("cur", background=SEL)
        self.t.tag_configure("subj", font=FONT_B, spacing1=6, lmargin1=12, lmargin2=12)
        self.t.tag_configure("sub", foreground=MUTED, font=SMALL, spacing3=6, lmargin1=12, lmargin2=12)
        self.t.tag_configure("dir", foreground=MUTED, spacing1=4, spacing3=4, lmargin1=12, lmargin2=12)
        self.t.tag_configure("name", foreground=FG, spacing1=4, spacing3=4, lmargin1=12, lmargin2=12)
        self.t.bind("<Button-1>", self.click)
        self.t.bind("<Double-Button-1>", lambda e: "break")
        self.t.bind("<B1-Motion>", lambda e: "break")
        self.t.bind("<Up>", lambda e: self.step(-1))
        self.t.bind("<Down>", lambda e: self.step(1))
        self.t.bind("<Configure>", self.resized)

    def width(self):
        w = self.t.winfo_width()
        return w if w > 80 else 300

    def resized(self, e):
        self.t.configure(tabs=(max(40, e.width - 22), "right"))  # icône d'état alignée à droite, avant la barre de défilement
        if self.fit and abs(e.width - self.w) > 8:  # les chemins longs sont raccourcis à la largeur : on redessine
            self.w = e.width
            if self.job:
                self.after_cancel(self.job)
            self.job = self.after(120, self.redraw)

    def draw(self, build):
        self.t.config(state=tk.NORMAL)
        build(self.t, self.width())
        self.t.config(state=tk.DISABLED)

    def redraw(self):
        self.job = None
        self.t.config(state=tk.NORMAL)
        self.t.delete("1.0", tk.END)
        self.t.config(state=tk.DISABLED)
        for build in self.builds:
            self.draw(build)
        if self.cur is not None:
            self.t.tag_add("cur", f"{self.cur * self.per + 1}.0", f"{(self.cur + 1) * self.per + 1}.0")

    def clear(self):
        self.ids, self.builds, self.cur = [], [], None
        self.t.config(state=tk.NORMAL)
        self.t.delete("1.0", tk.END)
        self.t.config(state=tk.DISABLED)

    def add(self, iid, build):  # build(texte, largeur) écrit exactement `per` lignes
        self.ids.append(iid)
        self.builds.append(build)
        self.draw(build)

    def selection(self):
        return (self.ids[self.cur],) if self.cur is not None else ()

    def set(self, n, notify=True):
        n = max(0, min(n, len(self.ids) - 1))
        self.t.tag_remove("cur", "1.0", tk.END)
        self.t.tag_add("cur", f"{n * self.per + 1}.0", f"{(n + 1) * self.per + 1}.0")
        self.t.see(f"{n * self.per + 1}.0")
        self.t.xview_moveto(0)  # see() décale sinon la liste vers la droite : le texte collerait au bord
        changed, self.cur = n != self.cur, n
        if notify and self.cb and changed:
            self.cb(self.ids[n])

    def click(self, e):
        self.t.focus_set()
        n = (int(self.t.index(f"@{e.x},{e.y}").split(".")[0]) - 1) // self.per
        if n < len(self.ids):
            self.set(n)
        return "break"

    def step(self, delta):
        if self.ids:
            self.set((self.cur if self.cur is not None else -1 if delta > 0 else 1) + delta)
        return "break"


def commit_row(subject, author, when_):  # titre en gras, puis pastille · auteur • date
    def build(t, width):
        t.insert(tk.END, subject + "\n", "subj")
        start = t.index("end-1c")
        t.image_create(tk.END, image=avatar(author), padx=2)
        t.insert(tk.END, f"  {author} • {when_}\n")
        t.tag_add("sub", start, "end-1c")
    return build


FILE_ICON = {"M": ("▣", "#d29922"), "A": ("⊞", "#3fb950"), "D": ("⊟", "#f85149"), "R": ("➜", "#a371f7"), "C": ("➜", "#a371f7")}


FONT_M = {}


def fit_dir(head, name, width):  # dossier coupé par le début ("…/com/ibonia/") pour que le nom du fichier reste visible
    f = FONT_M.setdefault("f", tkfont.Font(font=FONT))
    room = width - 12 - 30 - f.measure(name)  # marge gauche, icône + barre de défilement
    if f.measure(head) <= room:
        return head
    cut = head
    while cut and f.measure("…" + cut) > room:
        cut = cut[1:]
    return "…" + cut if cut else ""


def file_row(code, path):  # dossier estompé + nom en clair, icône d'état à droite
    def build(t, width):
        head, _, name = path.rpartition("/")
        head = fit_dir(head + "/", name, width) if head else ""
        if head:
            t.insert(tk.END, head, "dir")
        t.insert(tk.END, name, "name")
        icon, color = FILE_ICON.get(code, ("●", MUTED))
        t.tag_configure(f"ic{code}", foreground=color)
        t.insert(tk.END, "\t" + icon + "\n", (f"ic{code}", "name"))
    return build


root = tk.Tk()
root.geometry("1280x840")
root.minsize(1000, 660)
root.configure(bg=BG)
root.protocol("WM_DELETE_WINDOW", quit_app)
st = ttk.Style()
st.theme_use("clam")
st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26, borderwidth=0, font=FONT,
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
as_lbl, count_lbl, status, diff_dir, diff_name, diff_add, diff_del, files_lbl, h_title, h_author, h_sha, h_add, h_del = (tk.StringVar() for _ in range(13))


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
    label(f, text=caption.upper(), bg=TOP, fg=MUTED, font=SMALL_B).pack(fill=tk.X, pady=(0, 2))
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
r_u = section("Utilisateur")
uav = tk.Label(r_u, bg=TOP)
uav.pack(side=tk.LEFT, padx=(0, 6))
ucombo = ttk.Combobox(r_u, state="readonly", font=FONT_B, width=14)
ucombo.pack(side=tk.LEFT)
ucombo.bind("<<ComboboxSelected>>", switch_user)
btn(r_u, "＋", lambda: profile_dialog(False), padx=8, pady=3).pack(side=tk.LEFT, padx=(6, 0))
btn(r_u, "✎", lambda: profile_dialog(True), padx=8, pady=3).pack(side=tk.LEFT, padx=(4, 0))
btn(r_u, "✕", delete_user, padx=8, pady=3).pack(side=tk.LEFT, padx=(4, 0))
r3 = tk.Frame(tb, bg=TOP)
r3.pack(side=tk.RIGHT, padx=14, pady=12)
tb_push = btn(r3, "Push", lambda: sync("Push", "push", "-u", "origin", "HEAD"), padx=12, pady=5)
tb_push.pack(side=tk.RIGHT, padx=(6, 0))
tb_pull = btn(r3, "Pull", lambda: sync("Pull", "pull", "--ff-only"), padx=12, pady=5)
tb_pull.pack(side=tk.RIGHT, padx=(6, 0))
tb_fetch = btn(r3, "Fetch", lambda: sync("Fetch", "fetch", "--prune"), padx=12, pady=5)
tb_fetch.pack(side=tk.RIGHT)
tb_mr = btn(r3, "⎇ Créer une MR", merge_dialog, PURPLE, "white", FONT_B, padx=12, pady=5)
tb_mr.pack(side=tk.RIGHT, padx=(0, 18))

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
tabs = tk.Frame(left, bg=BG, highlightthickness=1, highlightbackground=BORDER)
tabs.pack(fill=tk.X)
tab_bars = {}


def make_tab(which, text):  # onglet à plat avec filet bleu sous l'actif, comme GitHub Desktop
    f = tk.Frame(tabs, bg=BG)
    f.pack(side=tk.LEFT, fill=tk.X, expand=True)
    lab = tk.Label(f, text=text, bg=BG, fg=FG if which == "chg" else MUTED, font=FONT_B, pady=7, cursor="hand2")
    lab.pack(fill=tk.X)
    tab_bars[which] = tk.Frame(f, bg=BLUE if which == "chg" else BG, height=2)
    tab_bars[which].pack(fill=tk.X)
    lab.bind("<Button-1>", lambda e: tab(which))
    return lab


tab_chg, tab_hist, tab_pipe, tab_mr = make_tab("chg", "Changements"), make_tab("hist", "Historique"), make_tab("pipe", "Pipelines"), make_tab("mr", "MR")

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
      fg=MUTED, font=SMALL).pack(fill=tk.X, padx=14, pady=(3, 6))
form = tk.Frame(chg_frame, bg=BG)
form.pack(fill=tk.X, padx=12)
trow = tk.Frame(form, bg=BG)
trow.pack(fill=tk.X)
label(trow, text="Titre", fg=MUTED).pack(side=tk.LEFT)
ai_btn = btn(trow, "✨ Générer (IA)", ai, PURPLE, "white", FONT_B, padx=10, pady=2)
ai_btn.pack(side=tk.RIGHT)
btn(trow, "✕", lambda: (summary.delete(0, tk.END), desc.delete("1.0", tk.END), summary.focus_set()), padx=8, pady=2).pack(side=tk.RIGHT, padx=(0, 6))
summary = field(tk.Entry, form)
summary.pack(fill=tk.X, pady=(3, 8), ipady=6)
label(form, text="Description", fg=MUTED).pack(fill=tk.X)
desc = field(tk.Text, form, height=4, wrap=tk.WORD, padx=6, pady=6)
desc.pack(fill=tk.X, pady=(3, 8))
label(form, textvariable=as_lbl, fg=MUTED, font=SMALL).pack(fill=tk.X, pady=(0, 6))
crow = tk.Frame(form, bg=BG)
crow.pack(fill=tk.X)
commit_btn = btn(crow, "", lambda: commit(False), BLUE, "white", FONT_B, pady=8)
commit_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
push_btn = btn(crow, "Commit & Push", lambda: commit(True), GREEN, "white", FONT_B, padx=14, pady=8)
push_btn.pack(side=tk.LEFT, padx=(6, 0))

# --- onglet Historique : recherche + liste des commits (les fichiers et le diff sont dans le volet de droite)
hist_frame = tk.Frame(left, bg=BG)
hbar = tk.Frame(hist_frame, bg=BG)
hbar.pack(fill=tk.X, padx=12, pady=(8, 8))
label(hbar, text="🔍").pack(side=tk.LEFT, padx=(0, 6))
hflt = field(tk.Entry, hbar)
hflt.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
hflt.bind("<KeyRelease>", fill_commits)
clist = RowList(hist_frame, per=2, select=on_commit)
clist.pack(fill=tk.BOTH, expand=True)

# --- onglet Pipelines : liste des pipelines, jobs du pipeline choisi ; le log du job s'affiche à droite
pipe_frame = tk.Frame(left, bg=BG)
pipe_info = tk.StringVar()
prow = tk.Frame(pipe_frame, bg=BG)
prow.pack(fill=tk.X, padx=12, pady=(0, 6))
btn(prow, "Actualiser", refresh_pipes, padx=10, pady=2).pack(side=tk.LEFT)
btn(prow, "Ouvrir ↗", open_web, padx=10, pady=2).pack(side=tk.LEFT, padx=4)
btn(prow, "Token…", lambda: set_token(), padx=10, pady=2).pack(side=tk.LEFT)
label(prow, textvariable=pipe_info, fg=MUTED).pack(side=tk.RIGHT)
jbox, jtree = make_tree(pipe_frame, ("st", "name", "stage", "dur"), height=8, selectmode="browse")
jbox.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(2, 8))
label(pipe_frame, text="Jobs du pipeline", fg=MUTED).pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(6, 0))
pbox, ptree = make_tree(pipe_frame, ("st", "ref", "info", "when"), selectmode="browse")
pbox.pack(fill=tk.BOTH, expand=True, padx=12)
for t_, cols in ((ptree, (("st", "", 34, False), ("ref", "Branche", 110, False), ("info", "Pipeline", 160, True), ("when", "Date", 90, False))),
                 (jtree, (("st", "", 34, False), ("name", "Job", 170, True), ("stage", "Stage", 90, False), ("dur", "Durée", 80, False)))):
    for c, text, w, stretch in cols:
        t_.heading(c, text=text, anchor="w")
        t_.column(c, width=w, stretch=stretch, anchor="center" if c == "st" else "w")
    for tag, color in (("ok", "#56d364"), ("bad", RED), ("run", BLUE), ("wait", AMBER), ("skip", MUTED)):
        t_.tag_configure(tag, foreground=color)
ptree.bind("<Double-Button-1>", lambda e: open_web("p"))
jtree.bind("<Double-Button-1>", lambda e: open_web("j"))
ptree.bind("<<TreeviewSelect>>", on_pipe)
jtree.bind("<<TreeviewSelect>>", on_job)

# --- onglet MR : les merge requests créées par l'utilisateur actif ; le détail s'affiche à droite
mr_frame = tk.Frame(left, bg=BG)
mr_info = tk.StringVar()
mrow = tk.Frame(mr_frame, bg=BG)
mrow.pack(fill=tk.X, padx=12, pady=(0, 6))
btn(mrow, "Actualiser", refresh_mrs, padx=10, pady=2).pack(side=tk.LEFT)
btn(mrow, "Ouvrir ↗", open_mr, padx=10, pady=2).pack(side=tk.LEFT, padx=4)
btn(mrow, "Token…", lambda: set_token(), padx=10, pady=2).pack(side=tk.LEFT)
label(mrow, textvariable=mr_info, fg=MUTED).pack(side=tk.RIGHT)
mbox, mtree = make_tree(mr_frame, ("st", "title", "br", "when"), selectmode="browse")
mbox.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
for c, text, w, stretch in (("st", "", 34, False), ("title", "Merge request", 200, True), ("br", "Branches", 170, False), ("when", "Date", 90, False)):
    mtree.heading(c, text=text, anchor="w")
    mtree.column(c, width=w, stretch=stretch, anchor="center" if c == "st" else "w")
for tag, color in (("ok", "#56d364"), ("run", BLUE), ("wait", AMBER), ("skip", MUTED)):
    mtree.tag_configure(tag, foreground=color)
mtree.bind("<Double-Button-1>", open_mr)
mtree.bind("<<TreeviewSelect>>", on_mr)

# --- volet droit : aperçu du diff
hhead = tk.Frame(right, bg=TOP, highlightthickness=1, highlightbackground=BORDER)  # en-tête du commit : affiché dans Historique seulement
h_titlebar = tk.Frame(hhead, bg=TOP)
h_titlebar.pack(fill=tk.X)
label(h_titlebar, textvariable=h_title, bg=TOP, font=BIG, padx=16, pady=8).pack(side=tk.LEFT)
h_body = tk.Text(hhead, wrap="word", bg=PANEL, fg=FG, font=MONO, relief=tk.FLAT, bd=0, highlightthickness=0, height=1, padx=10, pady=8,
                 state=tk.DISABLED, cursor="arrow", selectbackground=SEL)
hmeta = tk.Frame(hhead, bg=TOP)
hmeta.pack(fill=tk.X, padx=16, pady=(0, 10))
h_avatar = tk.Label(hmeta, bg=TOP)
h_avatar.pack(side=tk.LEFT)
label(hmeta, textvariable=h_author, bg=TOP, padx=6).pack(side=tk.LEFT)
label(hmeta, textvariable=h_sha, bg=TOP, fg=MUTED, font=MONO).pack(side=tk.LEFT, padx=(8, 0))
copy_lbl = tk.Label(hmeta, text="⧉", bg=TOP, fg=MUTED, font=FONT, cursor="hand2")
copy_lbl.pack(side=tk.LEFT, padx=6)
copy_lbl.bind("<Button-1>", lambda e: (root.clipboard_clear(), root.clipboard_append(HIST.get("full", "")), status.set("Hash copié")))
label(hmeta, textvariable=h_del, bg=TOP, fg=RED, font=MONO_B).pack(side=tk.RIGHT)
label(hmeta, textvariable=h_add, bg=TOP, fg="#56d364", font=MONO_B).pack(side=tk.RIGHT, padx=6)
rbody = tk.PanedWindow(right, orient=tk.HORIZONTAL, bg=BORDER, sashwidth=3, bd=0, sashrelief=tk.FLAT)
rbody.pack(fill=tk.BOTH, expand=True)
fpanel = tk.Frame(rbody, bg=BG)  # colonne « N fichiers modifiés » : Historique seulement
tk.Label(fpanel, textvariable=files_lbl, bg=TOP, fg=FG, font=FONT_B, pady=7).pack(fill=tk.X)
flist = RowList(fpanel, per=1, select=on_hfile, fit=True)
flist.pack(fill=tk.BOTH, expand=True)
dwrap = tk.Frame(rbody, bg=BG)
rbody.add(dwrap, stretch="always", minsize=300)
dhead = tk.Frame(dwrap, bg=TOP, highlightthickness=1, highlightbackground=BORDER)
dhead.pack(fill=tk.X)
tk.Frame(dhead, bg=TOP, width=16).pack(side=tk.LEFT)
label(dhead, textvariable=diff_dir, bg=TOP, fg=MUTED, pady=12).pack(side=tk.LEFT)
label(dhead, textvariable=diff_name, bg=TOP, font=FONT_B, pady=12).pack(side=tk.LEFT)
label(dhead, textvariable=diff_del, bg=TOP, fg=RED, font=MONO_B).pack(side=tk.RIGHT, padx=(0, 16))
label(dhead, textvariable=diff_add, bg=TOP, fg="#56d364", font=MONO_B).pack(side=tk.RIGHT, padx=6)
dframe = tk.Frame(dwrap, bg=PANEL)
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
d.tag_configure("empty", foreground=MUTED, font=(SANS, SZ + 1), lmargin1=70, spacing1=120)
for _t, _c in (("kw", "#ff7b72"), ("str", "#a5d6ff"), ("num", "#79c0ff"), ("typ", "#ffa657"), ("ann", "#d2a8ff"),
               ("tag", "#7ee787"), ("attr", "#79c0ff")):  # couleurs façon GitHub dark
    d.tag_configure(_t, foreground=_c)
d.tag_configure("com", foreground="#8b949e", font=MONO_I)
d.tag_configure("ln", foreground="#6e7681")  # créé en dernier : sa couleur l'emporte sur celle de add/del
btns = [ai_btn, commit_btn, push_btn, tb_fetch, tb_pull, tb_push, tb_mr]

try:
    with open(CONF, encoding="utf-8") as fh:
        repos = [p for p in json.load(fh) if os.path.isdir(p)]  # oublie les dossiers disparus
except (OSError, ValueError, TypeError):
    repos = []
load_profiles()
refresh_users()
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
