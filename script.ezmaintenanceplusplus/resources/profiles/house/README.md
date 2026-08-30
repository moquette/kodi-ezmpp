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
