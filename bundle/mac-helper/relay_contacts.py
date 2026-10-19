"""iMessage Relay v1.4.7 -- look a name up in the Mac's Contacts (for a reply to someone new).

When the owner starts a reply with a name that is on no relay list ("Jordan Rivera: hey"), the host
asks the watcher (it holds Full Disk Access, which covers the Contacts database) to look that name up.
Only an EXACT full-name match, or a first name that matches exactly one person, counts. The host then
asks the owner to confirm with /yes (2 minutes) before a new 1:1 is started; nothing is ever sent on a
guess. Ambiguous names list up to 3 names, never numbers. Read-only (sqlite mode=ro); never logged.
"""
import glob
import os
import re
import sqlite3

import relay_common as rc

MAX_RESULTS = 5


def _globs():
    import imessage_relay_watch as w      # same AddressBook paths the watcher uses for display names
    return w.ADDRESSBOOK_GLOBS


def _people(globs):
    """{record key: {'name','first','handles':[(kind,label,handle)]}} from every AddressBook source."""
    people = {}
    for pattern in globs:
        for path in glob.glob(pattern):
            try:
                con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                for table, col, kind in (('ZABCDPHONENUMBER', 'ZFULLNUMBER', 'phone'), ('ZABCDEMAILADDRESS', 'ZADDRESS', 'email')):
                    try:
                        cols = {r[1] for r in con.execute('PRAGMA table_info(%s)' % table)}
                        lab = 'x.ZLABEL' if 'ZLABEL' in cols else 'NULL'
                        rows = con.execute('SELECT r.Z_PK, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, x.%s, %s FROM %s x '
                                           'JOIN ZABCDRECORD r ON r.Z_PK = x.ZOWNER' % (col, lab, table)).fetchall()
                    except sqlite3.Error:
                        continue
                    for pk, first, last, org, value, label in rows:
                        name = ' '.join(p for p in (first, last) if p) or (org or '')
                        if not name or not value:
                            continue
                        e = people.setdefault((path, pk), {'name': name.strip(), 'first': str(first or '').strip(),
                                                           'nick': '', 'handles': []})
                        e['handles'].append((kind, str(label or ''), str(value)))
                try:                                  # v1.4.2: nicknames count like first names (always asked)
                    rcols = {r[1] for r in con.execute('PRAGMA table_info(ZABCDRECORD)')}
                    if 'ZNICKNAME' in rcols:
                        for pk, nick in con.execute('SELECT Z_PK, ZNICKNAME FROM ZABCDRECORD WHERE ZNICKNAME IS NOT NULL'):
                            if (path, pk) in people:
                                people[(path, pk)]['nick'] = str(nick or '').strip()
                except sqlite3.Error:
                    pass
            finally:
                con.close()
    return people


def best_handle(handles, cc='1'):
    """The handle a new iMessage most likely reaches: a mobile/iPhone number, else any number, else an email."""
    def score(h):
        kind, label, _ = h
        low = label.lower()
        if kind == 'phone' and ('mobile' in low or 'iphone' in low):
            return 0
        return 1 if kind == 'phone' else 2
    for h in sorted(handles, key=score):
        n = rc.normalize_handle(h[2], cc)
        if rc.valid_handle(n):
            return n
    return ''


def lookup_many(names, cfg, globs=None, fuzzy=None):
    """v1.4.2: several candidate names in one pass over Contacts (read once). `fuzzy[i]` allows close-typo
    matches for names[i]. Returns {'ok':True,'results':[{'matches':[...]}, ...]}."""
    people = list(_people(globs or _globs()).values())
    fz = list(fuzzy or [])
    res = [lookup(n, cfg, people=people, fuzzy=bool(fz[i]) if i < len(fz) else False)
           for i, n in enumerate(list(names or [])[:4])]
    return {'ok': True, 'results': res}


def lookup(name, cfg, globs=None, people=None, fuzzy=False):
    """Exact full name (case/space-insensitive), else a unique-or-several first-name match.
    Returns {'ok':True,'matches':[{'name','handle','exact','handles'}]} (at most MAX_RESULTS): `exact` is a
    full-name match, `handles` every valid number/email on that card (v1.4.1: one card with one handle
    and an exact name may be texted without a /yes). The owner's own handles are never returned.
    v1.4.2: a nickname counts like a first name; with `fuzzy`, when nothing else matches, close typos of a
    full name (or of a first name/nickname, for a one-word query) are returned marked 'fuzzy' (always asked)."""
    q = ' '.join(str(name or '').lower().split())
    if not q:
        return {'ok': True, 'matches': []}
    if people is None:
        people = _people(globs or _globs()).values()
    exact = [p for p in people if ' '.join(p['name'].lower().split()) == q]
    is_exact = bool(exact) and ' ' in q        # a one-word "name" is never an exact full-name match
    hits = exact or ([p for p in people if p['first'].lower() == q or p.get('nick', '').lower() == q]
                     if ' ' not in q else [])
    is_fuzzy = False
    if not hits and fuzzy:
        def keys(p):
            return [p['name']] if ' ' in q else [p['first'], p.get('nick', '')]
        hits = [p for p in people if max([rc.close_name_score(q, k) for k in keys(p) if k] or [0]) >= rc.FUZZY_MIN]
        is_fuzzy = bool(hits)
    own = rc.owner_handle_set(cfg)
    out, seen = [], set()
    for p in sorted(hits, key=lambda p: p['name'].lower()):
        h = best_handle(p['handles'], cfg.get('defaultCountryCode', '1'))
        if not h or h in own or h in seen:
            continue
        seen.add(h)
        cc = cfg.get('defaultCountryCode', '1')
        hs = []
        for kind, label, v in p['handles']:
            n = rc.normalize_handle(v, cc)
            if rc.valid_handle(n) and n not in own and n not in [x['handle'] for x in hs]:
                hs.append({'handle': n, 'label': re.sub(r'[^A-Za-z ]', '', label).strip().lower()[:12]})
        out.append({'name': re.sub(r'[\x00-\x1f\x7f]', ' ', p['name'])[:60], 'handle': h, 'exact': is_exact,
                    'handles': hs, 'fuzzy': is_fuzzy})
        if len(out) >= MAX_RESULTS:
            break
    return {'ok': True, 'matches': out}
