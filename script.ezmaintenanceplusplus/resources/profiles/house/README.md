# The House settings profile bundle

Every value the Apply Settings Profile flow applies lives HERE, as data. The
code in `resources/lib/modules/profile.py` carries no payload literal.

Layout and rules (the loader enforces all of them; see
`docs/settings-profile-plan.md` section 7.1 and `tests/test_settings_profile.py`):

- `settings.d/*.xml`: class A fragments, standalone `<settings version="2">`
  documents merged in glob order. Value from the LAST occurrence of an id,
  position from the FIRST (so an overlay override can never move a parent
  after its dependent). No `default="true"`, no never-apply id
  (`_kodisettings._BOOT_STATE_ONLY`).
- `sources.xml`: class C source ENTRIES only; merged additively, never copied.
  Trailing slash required; no port on nfs paths.
- `addons.list` + `addons/*.zip`: class D, staged from the official hub zips
  and enabled through Kodi; the repository enables LAST.
- `nodes.d/*.xml`: guisettings FILE NODES outside the `<setting id>` space
  (`<nodes><node path="general/settinglevel">3</node></nodes>`). These cannot
  be live-set (no JSON-RPC, builtin or Python setter) and cannot be
  file-written while Kodi runs (the clean-close flush re-serializes them from
  live memory - measured 2026-08-30), so apply() only ARMS them; the service
  writes them in its abort window, after Kodi's one "Saving settings" flush,
  and the boot check verifies. Same merge rules as `settings.d`. Comments are
  fine here: only this add-on parses these files, never Kodi.
- `RssFeeds.xml`: the curated feed list, a WHOLE-FILE userdata payload carried
  verbatim and written byte-idempotently (Kodi writes that file on first run
  only and its shutdown flush never touches it - measured 2026-08-30).
- `addon_data/<id>/*.xml` (base, device-neutral): a third-party add-on's
  settings values, written BEFORE that add-on is enabled and vectored on tvOS
  (`nsud._should_vector` covers `addon_data/<id>/settings.xml`). Values-level
  idempotent: once the owner add-on runs, Kodi rewrites the file with every
  default filled in, so the re-run compare is per-id against the bundle's
  pairs, never byte equality. Same comment ban as the overlay addon_data.
  Values documents carry `version="2"`: any non-zero version takes Kodi's
  standard load path and 2 is accepted by every Kodi this fleet has run
  (CSettingsManager MinimumSupportedVersion=0; the ts1 capture's newer build
  writes 4, which OLDER builds reject, so 4 is never carried here).
- `skinsettings.xml`: SKIN bools for whichever skin is live at apply time, set
  via `Skin.SetBool` and persisted by the clean-shutdown flush (the
  `_apply_skin_settings` mechanism; a file write would be clobbered by that
  same flush). Parsed only by this add-on, so comments are fine. Bools only,
  by design - a string payload adds `Skin.SetString` with the builtin-quoting
  guard the day one exists.
- `overlays/<class>/`: fireos, tvos, bench. Device-scoped leaves (the backup
  folder) exist ONLY here, and loading FAILS when the running class has no
  overlay, so an Apple TV can never silently inherit the Fire TV folder. The
  bench overlay deliberately reproduces the fireos leaf, matching how the
  bench has always been seeded.
- Comment nodes are rejected in any `addon_data` document at load: Kodi's
  `CAddonSettings::Load` calls `Attribute("id")` on every child without
  checking it is an element, and a comment node is a SIGABRT on the first
  `getSetting()`.

The 2026-08-30 bundle was authored from `bootstrapper/settings/` as it stood
that day (bootstrapper itself is archived) and every class A value was
verified live-settable on a first-run Kodi 22 Piers bench:
`docs/settings-profile-experiments-2026-08-30.md`.
