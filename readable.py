"""Turns the extracted databases (messages, WhatsApp, Safari, notes, ...) into pages you can open in a browser."""
import gzip
import html
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VID = {".mp4", ".mov", ".m4v"}
AUD = {".mp3", ".m4a", ".aac", ".wav", ".opus", ".caf", ".amr"}

CSS = """body{font:15px system-ui,sans-serif;background:#111b21;color:#e9edef;margin:0;padding:16px;max-width:900px;margin:auto}
a{color:#7cc4ff}h1{font-size:20px}.m{display:flex;flex-direction:column;margin:6px 0}.m.me{align-items:flex-end}
.b{max-width:75%;padding:7px 10px;border-radius:10px;background:#202c33;white-space:pre-wrap;word-wrap:break-word}
.me .b{background:#005c4b}.who{font-size:12px;color:#53bdeb;margin:0 4px}.t{font-size:11px;color:#8696a0;margin:1px 4px}
img,video{max-width:280px;max-height:280px;display:block;margin-top:4px;border-radius:6px}.sys{text-align:center;color:#8696a0;font-size:12px;margin:8px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #26343c;padding:5px 8px;text-align:left;vertical-align:top;font-size:14px}
input{width:100%;padding:8px;margin:8px 0;background:#202c33;color:#e9edef;border:1px solid #333;border-radius:6px}
.note{background:#202c33;border-radius:8px;padding:10px 14px;margin:10px 0;white-space:pre-wrap}"""

FILTER_JS = """<script>function f(v){v=v.toLowerCase();for(const r of document.querySelectorAll('tr.r,.note'))
r.style.display=r.textContent.toLowerCase().includes(v)?'':'none'}</script>"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def page(title, body, filt=False):
    box = '<input placeholder="Search..." oninput="f(this.value)">' if filt else ""
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{esc(title)}</title><style>{CSS}</style><body><h1>{esc(title)}</h1>{box}{body}{FILTER_JS}</body>')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr class=r>" + "".join(f"<td>{c if isinstance(c, Raw) else esc(c)}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


class Raw(str):
    """HTML that must not be escaped."""


def when(v):
    if v is None:
        return ""
    try:
        v = float(v)
        if abs(v) > 1e12:
            v /= 1e9
        return (EPOCH + timedelta(seconds=v)).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().rstrip(". ")
    return name or "_"


def connect(path):
    return sqlite3.connect(path)


def digits(s):
    return re.sub(r"\D", "", s or "")[-10:]


# ---------- contacts / names ----------
class Names:
    def __init__(self, out):
        self.phone, self.mail = {}, {}
        db = os.path.join(out, "Contacts", "AddressBook.sqlitedb")
        if not os.path.exists(db):
            return
        con = connect(db)
        people = {}
        for rid, first, last, org in con.execute("SELECT ROWID, First, Last, Organization FROM ABPerson"):
            people[rid] = " ".join(x for x in (first, last) if x) or org or ""
        for rid, prop, val in con.execute("SELECT record_id, property, value FROM ABMultiValue WHERE property IN (3,4)"):
            name = people.get(rid)
            if name and val:
                if prop == 3 and digits(val):
                    self.phone[digits(val)] = name
                elif prop == 4:
                    self.mail[val.lower()] = name
        con.close()

    def get(self, ident, default=None):
        if not ident:
            return default
        if "@" in ident and not ident.endswith(("@s.whatsapp.net", "@g.us", "@lid")):
            return self.mail.get(ident.lower(), ident)
        user = ident.split("@")[0]
        return self.phone.get(digits(user)) or default or (("+" + user) if user.isdigit() else ident)


B64 = re.compile(r"^[A-Za-z0-9+/=]{16,}$")


class WaNames:
    """Works out who a WhatsApp sender is: address book, then WhatsApp names, then the number."""

    def __init__(self, out, names, push):
        self.names, self.push, self.lid = names, push, {}
        db = os.path.join(out, "WhatsApp", "Database", "LID.sqlite")
        if os.path.exists(db):
            try:
                con = connect(db)
                for ident, phone, disp in con.execute("SELECT ZIDENTIFIER, ZPHONENUMBER, ZDISPLAYNAME FROM ZWAZACCOUNT"):
                    if ident:
                        self.lid[ident.split("@")[0]] = (phone, disp)
                con.close()
            except Exception:
                pass

    def user(self, user):
        """Best name for a bare id (phone number or privacy id), or None."""
        phone, disp = self.lid.get(user, (None, None))
        number = phone or (user if user.isdigit() and len(user) < 15 else None)
        return (self.names.phone.get(digits(number)) if number else None) or disp or \
            self.push.get(user + "@lid") or self.push.get(user + "@s.whatsapp.net") or \
            (("+" + str(number).lstrip("+")) if number else None)

    def who(self, jid, contact_name, push_name):
        found = self.user(jid.split("@")[0]) if jid else None
        if found and not found.startswith("+"):
            return found
        if contact_name and not B64.match(contact_name):
            return contact_name
        if push_name and not B64.match(push_name):
            return push_name
        return found or "Unknown member"

    def mentions(self, text):
        return re.sub(r"@(\d{8,})", lambda m: "@" + (self.user(m.group(1)) or m.group(1)), text) if text else text


# ---------- attachments ----------
def media_html(out, root, rel, label=""):
    """rel: path inside `root` (e.g. Messages or WhatsApp). The page lives in root/Chats."""
    parts = [safe(p) for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts or not os.path.exists(os.path.join(out, root, *parts)):
        return Raw(f"<i>[file not in backup: {esc(label or rel)}]</i>")
    href = "../" + "/".join(quote(p) for p in parts)
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext in IMG:
        return Raw(f'<a href="{href}"><img loading="lazy" src="{href}"></a>')
    if ext in VID:
        return Raw(f'<video controls preload="none" src="{href}"></video>')
    if ext in AUD:
        return Raw(f'<audio controls preload="none" src="{href}"></audio>')
    return Raw(f'<a href="{href}">{esc(parts[-1])}</a>')


def bubble(who, mine, text, att, date, show_who):
    inner = esc(text) + "".join("<br>" + a for a in att)
    name = f'<div class="who">{esc(who)}</div>' if show_who and not mine else ""
    return f'<div class="m {"me" if mine else ""}">{name}<div class="b">{inner}</div><div class="t">{esc(date)}</div></div>'


def write_chats(folder, items, title, link_back):
    """items: list of (chat_key, chat_title, [html bubbles], last_date). Writes one page per chat + an index."""
    index = []
    for key, name, bubbles, last in sorted(items, key=lambda i: i[3] or "", reverse=True):
        fname = f"{safe(name)[:70]} [{key}].html"
        write(os.path.join(folder, fname), page(name, "".join(bubbles)))
        index.append((Raw(f'<a href="{quote(fname)}">{esc(name)}</a>'), len(bubbles), last))
    write(os.path.join(folder, "index.html"), page(title, table(["Chat", "Messages", "Last message"], index), filt=True))


# ---------- Messages ----------
def decode_attr(blob):
    try:
        t = blob.split(b"NSString")[1][5:]
        n, s = (int.from_bytes(t[1:3], "little"), 3) if t[0] == 0x81 else (t[0], 1)
        return t[s:s + n].decode("utf-8", "ignore")
    except Exception:
        return ""


def messages(out, names, log):
    db = os.path.join(out, "Messages", "sms.db")
    if not os.path.exists(db):
        return
    con = connect(db)
    chats = {r[0]: (r[1], r[2]) for r in con.execute("SELECT ROWID, chat_identifier, display_name FROM chat")}
    people = {}
    for cid, hid in con.execute("SELECT j.chat_id, h.id FROM chat_handle_join j JOIN handle h ON h.ROWID=j.handle_id"):
        people.setdefault(cid, []).append(hid)
    atts = {}
    for mid, fn, mime, tn in con.execute("SELECT j.message_id, a.filename, a.mime_type, a.transfer_name "
                                         "FROM message_attachment_join j JOIN attachment a ON a.ROWID=j.attachment_id"):
        atts.setdefault(mid, []).append((fn, tn))
    rows = con.execute("""SELECT m.ROWID, m.date, m.is_from_me, h.id, m.text, m.attributedBody, cmj.chat_id
                          FROM message m LEFT JOIN handle h ON h.ROWID=m.handle_id
                          LEFT JOIN chat_message_join cmj ON cmj.message_id=m.ROWID ORDER BY m.date""").fetchall()
    con.close()
    per = {}
    for mid, date, mine, handle, text, blob, cid in rows:
        if not text and blob:
            text = decode_attr(blob)
        text = (text or "").replace("￼", "").strip()
        att = []
        for fn, tn in atts.get(mid, []):
            if fn and "Library/SMS/" in fn:
                att.append(media_html(out, "Messages", fn.split("Library/SMS/", 1)[1], tn or fn))
            else:
                att.append(Raw(f"<i>[attachment: {esc(tn or '')}]</i>"))
        if not text and not att:
            continue
        per.setdefault(cid, []).append((date, mine, handle, text, att))
    items = []
    for cid, msgs in per.items():
        ident, disp = chats.get(cid, ("unknown", ""))
        members = people.get(cid, [])
        group = len(members) > 1
        title = disp or (", ".join(names.get(m) for m in members) if group else names.get(ident, ident))
        bubbles = [bubble("Me" if mine else names.get(handle, ident), mine, text, att, when(date), group)
                   for date, mine, handle, text, att in msgs]
        items.append((cid if cid is not None else 0, title, bubbles, when(msgs[-1][0])))
    write_chats(os.path.join(out, "Messages", "Chats"), items, "Messages", "..")
    log(f"Messages: {len(rows)} messages in {len(items)} chats -> Messages\\Chats\\index.html")


# ---------- WhatsApp ----------
def whatsapp(out, names, log):
    db = os.path.join(out, "WhatsApp", "Database", "ChatStorage.sqlite")
    if not os.path.exists(db):
        return
    con = connect(db)
    chats = {r[0]: r[1:] for r in con.execute("SELECT Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE FROM ZWACHATSESSION")}
    push = {j: n for j, n in con.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME") if j and n}
    wn = WaNames(out, names, push)
    rows = con.execute("""SELECT m.ZCHATSESSION, m.ZMESSAGEDATE, m.ZISFROMME, m.ZMESSAGETYPE, m.ZTEXT, m.ZFROMJID,
                                 m.ZPUSHNAME, gm.ZMEMBERJID, gm.ZCONTACTNAME, mi.ZMEDIALOCALPATH, mi.ZTITLE
                          FROM ZWAMESSAGE m
                          LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                          LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                          ORDER BY m.ZMESSAGEDATE""").fetchall()
    con.close()
    per = {}
    for cid, date, mine, mtype, text, frm, pname, member, mname, media, mtitle in rows:
        pname_chat, jid, stype = chats.get(cid, (None, None, None))
        group = bool(jid and jid.endswith("@g.us"))
        sender_jid = member if group else (jid or frm)
        who = "Me" if mine else wn.who(sender_jid, mname, pname)
        text = wn.mentions((text or "").strip())
        att = []
        if media:
            att.append(media_html(out, "WhatsApp", media, mtitle or media))
        if text.startswith("{") and text.endswith("}"):
            text, mtype = "", 6  # group event data, not a real message
        if mtype == 6 or (mtype in (10, 14) and not text and not att):
            if not text and not att:
                continue
        if not text and not att:
            continue
        per.setdefault(cid, []).append((date, mine, who, text, att))
    items = []
    for cid, msgs in per.items():
        pname_chat, jid, stype = chats.get(cid, (None, None, None))
        title = pname_chat or names.get(jid, jid) or f"chat {cid}"
        group = bool(jid and jid.endswith("@g.us"))
        bubbles = [bubble(who, mine, text, att, when(date), group) for date, mine, who, text, att in msgs]
        items.append((cid, title, bubbles, when(msgs[-1][0])))
    write_chats(os.path.join(out, "WhatsApp", "Chats"), items, "WhatsApp chats", "..")
    log(f"WhatsApp: {len(rows)} messages in {len(items)} chats -> WhatsApp\\Chats\\index.html")


# ---------- Safari ----------
def safari(out, names, log):
    base = os.path.join(out, "Safari")
    hist = os.path.join(base, "History.db")
    if os.path.exists(hist):
        con = connect(hist)
        rows = con.execute("""SELECT v.visit_time, v.title, i.url FROM history_visits v
                              JOIN history_items i ON i.id = v.history_item ORDER BY v.visit_time DESC""").fetchall()
        con.close()
        write(os.path.join(base, "history.html"),
              page("Safari history", table(["Time", "Title", "URL"], [(when(t), ti, u) for t, ti, u in rows]), True))
        log(f"Safari: {len(rows)} history entries -> Safari\\history.html")
    bm = os.path.join(base, "Bookmarks.db")
    if os.path.exists(bm):
        con = connect(bm)
        rows = con.execute("""SELECT b.title, b.url, p.title FROM bookmarks b LEFT JOIN bookmarks p ON p.id = b.parent
                              WHERE b.type = 0 AND b.url IS NOT NULL AND COALESCE(b.deleted, 0) = 0""").fetchall()
        con.close()
        write(os.path.join(base, "bookmarks.html"),
              page("Safari bookmarks", table(["Title", "URL", "Folder"], rows), True))
        log(f"Safari: {len(rows)} bookmarks -> Safari\\bookmarks.html")


# ---------- Notes ----------
def varint(b, i):
    n = s = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return n, i


def pb_get(b, field):
    i = 0
    while i < len(b):
        tag, i = varint(b, i)
        wt = tag & 7
        if wt == 0:
            _, i = varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            n, i = varint(b, i)
            if tag >> 3 == field:
                return b[i:i + n]
            i += n
        else:
            return None
    return None


def note_text(data):
    raw = gzip.decompress(data)
    note = pb_get(pb_get(raw, 2), 3)  # document -> note -> text (field 2)
    return pb_get(note, 2).decode("utf-8", "replace").replace("￼", "[attachment]")


def notes(out, names, log):
    db = os.path.join(out, "Notes", "NoteStore.sqlite")
    if not os.path.exists(db):
        return
    con = connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")]
    mod = next((c for c in cols if c.startswith("ZMODIFICATIONDATE")), "NULL")
    cre = next((c for c in cols if c.startswith("ZCREATIONDATE")), "NULL")
    rows = con.execute(f"""SELECT n.Z_PK, n.ZTITLE1, n.{mod}, n.{cre}, f.ZTITLE2, d.ZDATA, n.ZISPASSWORDPROTECTED
                           FROM ZICNOTEDATA d JOIN ZICCLOUDSYNCINGOBJECT n ON n.Z_PK = d.ZNOTE
                           LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
                           WHERE COALESCE(n.ZMARKEDFORDELETION, 0) = 0
                           ORDER BY n.{mod} DESC""").fetchall()
    con.close()
    blocks, count, locked = [], 0, 0
    for pk, title, m, c, folder, data, protected in rows:
        title = title or "(untitled)"
        if protected:
            locked += 1
            blocks.append(f'<div class="note"><b>{esc(title)}</b> <i>(locked note - cannot be read from a backup)</i></div>')
            continue
        try:
            text = note_text(data)
        except Exception:
            continue
        count += 1
        blocks.append(f'<div class="note"><b>{esc(title)}</b><br><span class="t">{esc(folder or "")} · '
                      f'edited {esc(when(m))} · created {esc(when(c))}</span><br><br>{esc(text)}</div>')
        write(os.path.join(out, "Notes", "Readable", f"{safe(title)[:60]} [{pk}].txt"), text)
    write(os.path.join(out, "Notes", "notes.html"), page("Notes", "".join(blocks), True))
    log(f"Notes: {count} notes ({locked} locked) -> Notes\\notes.html")


# ---------- Contacts / calls ----------
def contacts(out, names, log):
    db = os.path.join(out, "Contacts", "AddressBook.sqlitedb")
    if not os.path.exists(db):
        return
    con = connect(db)
    info = {}
    for rid, prop, val in con.execute("SELECT record_id, property, value FROM ABMultiValue WHERE property IN (3,4)"):
        info.setdefault(rid, []).append(val)
    rows = [(" ".join(x for x in (f, l) if x) or o or "", o or "", ", ".join(v for v in info.get(rid, []) if v))
            for rid, f, l, o in con.execute("SELECT ROWID, First, Last, Organization FROM ABPerson")]
    con.close()
    rows.sort(key=lambda r: r[0].lower())
    write(os.path.join(out, "Contacts", "contacts.html"),
          page("Contacts", table(["Name", "Organization", "Phones / emails"], rows), True))
    log(f"Contacts: {len(rows)} -> Contacts\\contacts.html")


def calendar(out, names, log):
    db = os.path.join(out, "Calendar", "Calendar.sqlitedb")
    if not os.path.exists(db):
        return
    con = connect(db)
    rows = con.execute("""SELECT i.start_date, i.end_date, i.all_day, i.summary, i.description, c.title
                          FROM CalendarItem i LEFT JOIN Calendar c ON c.ROWID = i.calendar_id
                          WHERE i.summary IS NOT NULL AND i.start_date IS NOT NULL
                          ORDER BY i.start_date DESC""").fetchall()
    con.close()
    data = [(when(s)[:10] if allday else when(s), when(e)[:10] if allday else when(e), t, d or "", cal or "")
            for s, e, allday, t, d, cal in rows]
    write(os.path.join(out, "Calendar", "calendar.html"),
          page("Calendar events", table(["Start", "End", "Event", "Notes", "Calendar"], data), True))
    log(f"Calendar: {len(data)} events -> Calendar\\calendar.html")


def calls(out, names, log):
    db = os.path.join(out, "Call History", "CallHistory.storedata")
    if not os.path.exists(db):
        return
    con = connect(db)
    rows = []
    for date, dur, addr, orig, ans in con.execute(
            "SELECT ZDATE, ZDURATION, ZADDRESS, ZORIGINATED, ZANSWERED FROM ZCALLRECORD ORDER BY ZDATE DESC"):
        num = addr.decode("utf-8", "ignore") if isinstance(addr, bytes) else (addr or "")
        kind = "Outgoing" if orig else ("Incoming" if ans else "Missed")
        rows.append((when(date), names.get(num, num), kind, f"{int(dur or 0) // 60}m {int(dur or 0) % 60}s"))
    con.close()
    write(os.path.join(out, "Call History", "calls.html"),
          page("Call history", table(["Time", "Who", "Type", "Duration"], rows), True))
    log(f"Calls: {len(rows)} -> Call History\\calls.html")


# ---------- media galleries ----------
KINDS = {}
for _ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
    KINDS[_ext] = "p"
for _ext in (".heic", ".heif", ".heics"):
    KINDS[_ext] = "h"
for _ext in (".mp4", ".mov", ".m4v", ".3gp", ".avi"):
    KINDS[_ext] = "v"
for _ext in (".opus", ".m4a", ".mp3", ".caf", ".amr", ".aac", ".wav"):
    KINDS[_ext] = "a"
for _ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".vcf"):
    KINDS[_ext] = "d"
SKIP_DIRS = {"previews", "thumbnails", "stickercache"}

GALLERY_JS = r"""
const K={p:'Photos',h:'Photos (HEIC)',v:'Videos',a:'Audio',d:'Documents'};
let kind='all',q='',asc=false,list=[],shown=0,cur=-1;
const grid=document.getElementById('g'),cnt=document.getElementById('c');
const url=r=>r.split('/').map(encodeURIComponent).join('/');
const day=t=>new Date(t*1000).toLocaleString();
function filter(){const s=q.toLowerCase();
 list=D.filter(d=>(kind==='all'||d[1]===kind||(kind==='p'&&d[1]==='h'))&&(!s||(d[0]+' '+d[3]).toLowerCase().includes(s)));
 list.sort((a,b)=>asc?a[2]-b[2]:b[2]-a[2]);grid.innerHTML='';shown=0;more();
 cnt.textContent=list.length+' of '+D.length+' items';}
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){const el=e.target;io.unobserve(el);
 if(el.dataset.src){el.src=el.dataset.src;}}}),{rootMargin:'600px'});
function tile(d,i){const t=document.createElement('div');t.className='tile';const u=url(d[0]);
 const cap='<div class="cap">'+esc(d[3]||'')+'<br>'+day(d[2])+'</div>';
 if(d[1]==='p'){t.innerHTML='<img>'+cap;const im=t.firstChild;im.dataset.src=u;io.observe(im);im.onclick=()=>open(i);}
 else if(d[1]==='v'){t.innerHTML='<video controls preload="none"></video>'+cap;const v=t.firstChild;v.dataset.src=u;io.observe(v);}
 else if(d[1]==='a'){t.innerHTML='<div class="ico">Audio</div><audio controls preload="none" src="'+u+'"></audio>'+cap;}
 else{t.innerHTML='<a class="ico" href="'+u+'" target="_blank">'+(d[1]==='h'?'HEIC photo':'Document')+'<br><small>'+esc(d[0].split('/').pop())+'</small></a>'+cap;}
 return t;}
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function more(){const end=Math.min(shown+300,list.length);for(let i=shown;i<end;i++)grid.appendChild(tile(list[i],i));shown=end;}
new IntersectionObserver(es=>{if(es[0].isIntersecting&&shown<list.length)more();},{rootMargin:'800px'}).observe(document.getElementById('end'));
function open(i){cur=i;const d=list[i],lb=document.getElementById('lb');lb.style.display='flex';
 lb.innerHTML='<img src="'+url(d[0])+'"><div>'+esc(d[3]||'')+' · '+day(d[2])+' · <a href="'+url(d[0])+'" target="_blank">open file</a></div>';}
document.getElementById('lb').onclick=e=>{if(e.target.tagName!=='A')e.currentTarget.style.display='none';};
document.onkeydown=e=>{const lb=document.getElementById('lb');if(lb.style.display!=='flex')return;
 if(e.key==='Escape')lb.style.display='none';
 else if(e.key==='ArrowRight'){let j=cur+1;while(j<list.length&&list[j][1]!=='p')j++;if(j<list.length)open(j);}
 else if(e.key==='ArrowLeft'){let j=cur-1;while(j>=0&&list[j][1]!=='p')j--;if(j>=0)open(j);}};
document.querySelectorAll('[data-k]').forEach(b=>b.onclick=()=>{kind=b.dataset.k;filter();});
document.getElementById('s').oninput=e=>{q=e.target.value;filter();};
document.getElementById('o').onclick=e=>{asc=!asc;e.target.textContent=asc?'Oldest first':'Newest first';filter();};
filter();
"""

GALLERY_CSS = """.bar{position:sticky;top:0;background:#111b21;padding:8px 0;z-index:5}button{background:#202c33;color:#e9edef;border:1px solid #333;
border-radius:6px;padding:6px 12px;margin:2px;cursor:pointer}button:hover{background:#2a3942}
#g{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px}.tile{background:#202c33;border-radius:8px;padding:6px;overflow:hidden}
.tile img,.tile video{width:100%;height:170px;object-fit:cover;max-width:none;max-height:none;margin:0;cursor:zoom-in;background:#0b141a}
.tile audio{width:100%}.ico{display:flex;flex-direction:column;align-items:center;justify-content:center;height:90px;text-align:center;color:#7cc4ff;word-break:break-all}
.cap{font-size:11px;color:#8696a0;margin-top:4px;overflow:hidden;text-overflow:ellipsis}
#lb{display:none;position:fixed;inset:0;background:#000e;z-index:9;flex-direction:column;align-items:center;justify-content:center;gap:8px}
#lb img{max-width:95vw;max-height:88vh;object-fit:contain;max-height:88vh}"""


def build_gallery(out, source, title, labels, log):
    import json
    root = os.path.join(out, source)
    if not os.path.isdir(root):
        return
    items = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        for f in files:
            kind = KINDS.get(os.path.splitext(f)[1].lower())
            if not kind:
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel.startswith("Chats/") or rel.startswith("Readable/"):
                continue
            try:
                ts = int(os.path.getmtime(full))
            except OSError:
                continue
            items.append([rel, kind, ts, labels(rel)])
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    btns = "".join(f'<button data-k="{k}">{n}</button>' for k, n in
                   (("all", "All"), ("p", "Photos"), ("v", "Videos"), ("a", "Audio"), ("d", "Documents")))
    body = (f'<div class="bar">{btns}<button id="o">Newest first</button> <span id="c" class="t"></span>'
            f'<input id="s" placeholder="Search by chat or file name..."></div><div id="g"></div><div id="end"></div>'
            f'<div id="lb"></div><style>{GALLERY_CSS}</style><script>const D={data};{GALLERY_JS}</script>')
    write(os.path.join(root, "gallery.html"), page(title, body))
    counts = {}
    for it in items:
        counts[it[1]] = counts.get(it[1], 0) + 1
    log(f"{title}: {len(items)} items ({counts.get('p', 0) + counts.get('h', 0)} photos, "
        f"{counts.get('v', 0)} videos, {counts.get('a', 0)} audio, {counts.get('d', 0)} docs) -> {source}\\gallery.html")


def galleries(out, names, log):
    # WhatsApp: label each file with its chat
    wa_labels = {}
    db = os.path.join(out, "WhatsApp", "Database", "ChatStorage.sqlite")
    if os.path.exists(db):
        con = connect(db)
        for pname, jid in con.execute("SELECT ZPARTNERNAME, ZCONTACTJID FROM ZWACHATSESSION"):
            if jid:
                wa_labels[safe(jid)] = pname or names.get(jid, jid)
        con.close()
    build_gallery(out, "WhatsApp", "WhatsApp media",
                  lambda rel: wa_labels.get(rel.split("/")[1] if rel.startswith("Media/") and rel.count("/") > 1 else "", ""), log)

    # Messages: label each attachment with its chat
    sms_labels = {}
    db = os.path.join(out, "Messages", "sms.db")
    if os.path.exists(db):
        con = connect(db)
        members = {}
        for cid, hid in con.execute("SELECT j.chat_id, h.id FROM chat_handle_join j JOIN handle h ON h.ROWID=j.handle_id"):
            members.setdefault(cid, []).append(hid)
        for fn, ident, disp, cid in con.execute(
                """SELECT a.filename, c.chat_identifier, c.display_name, c.ROWID FROM attachment a
                   JOIN message_attachment_join j ON j.attachment_id=a.ROWID
                   JOIN chat_message_join cm ON cm.message_id=j.message_id JOIN chat c ON c.ROWID=cm.chat_id
                   WHERE a.filename IS NOT NULL"""):
            if "Library/SMS/" in fn:
                m = members.get(cid, [])
                sms_labels["/".join(safe(p) for p in fn.split("Library/SMS/", 1)[1].split("/"))] = \
                    disp or (", ".join(names.get(x) for x in m) if len(m) > 1 else names.get(ident, ident))
        con.close()
    build_gallery(out, "Messages", "Messages media", lambda rel: sms_labels.get(rel, ""), log)

    build_gallery(out, "Notes", "Notes media", lambda rel: "", log)


# ---------- entry point ----------
def make_readable(out, log):
    names = Names(out)
    for fn in (messages, whatsapp, safari, notes, contacts, calls, calendar, galleries):
        try:
            fn(out, names, log)
        except Exception as e:
            log(f"{fn.__name__}: could not build ({type(e).__name__}: {e})")
    links = [("Messages\\Chats\\index.html", "Messages"), ("WhatsApp\\Chats\\index.html", "WhatsApp chats"),
             ("Notes\\notes.html", "Notes"), ("Safari\\history.html", "Safari history"),
             ("Safari\\bookmarks.html", "Safari bookmarks"), ("Contacts\\contacts.html", "Contacts"),
             ("Call History\\calls.html", "Call history"), ("Calendar\\calendar.html", "Calendar"),
             ("WhatsApp\\gallery.html", "WhatsApp media gallery"), ("Messages\\gallery.html", "Messages media gallery"),
             ("Notes\\gallery.html", "Notes media gallery")]
    items = "".join(f'<p><a href="{quote(p.replace(chr(92), "/"))}">{t}</a></p>'
                    for p, t in links if os.path.exists(os.path.join(out, p)))
    write(os.path.join(out, "START HERE.html"), page("Your iPhone backup", items))
    log("Readable pages ready: open START HERE.html")
