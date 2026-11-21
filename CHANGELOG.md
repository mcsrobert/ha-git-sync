# Changelog

## [0.2.0](https://github.com/mcsrobert/ha-git-sync/compare/v0.1.0...v0.2.0) (2026-11-21)


### Features

* **check:** pin release assets by sha256 in hacs.yaml ([5834563](https://github.com/mcsrobert/ha-git-sync/commit/5834563ee2da31c4490fee7de3f266d3e1cba2ce))

## 0.1.0 (2026-11-21)


### ⚠ BREAKING CHANGES

* **check:** Move loose files under custom_components into their integration directory. The check action rejects files and hidden directories directly under either installation root.
* **hook:** Remove any GITSYNC_LINK override pointing at /config/.git-sync/current and use the image default incoming link so the hook can publish current after installation. A first rejected tip now holds startup until a valid deploy arrives. The hook also restarts Home Assistant after installing changed integrations even during an API outage, so allow for one restart after startup when first deploying custom integrations.

### Features

* deploy a Home Assistant configuration from Git ([b7d8c61](https://github.com/mcsrobert/ha-git-sync/commit/b7d8c618adbb403d3da3fdaba11a7d02ec820411))
* **hook:** restart Home Assistant when integrations change ([8b00aea](https://github.com/mcsrobert/ha-git-sync/commit/8b00aea84a59c770ebf4d5c36922f78f60b16036))


### Bug Fixes

* **check:** validate the installed layout and isolate local credentials ([d93a95f](https://github.com/mcsrobert/ha-git-sync/commit/d93a95f2ac4a44ddb9a0a4ba46ec6734ba1555e9))
* **hook:** count a restart only when Home Assistant closes the connection ([d17ea4d](https://github.com/mcsrobert/ha-git-sync/commit/d17ea4d1d0d792819d4c6fadad36688245c8d13b))
* **hook:** keep the last accepted deployment live ([0c1294c](https://github.com/mcsrobert/ha-git-sync/commit/0c1294c67b36fa873e173a1e61050a02705c8967))
* **hook:** restart only when Home Assistant runs the replaced integrations ([389d5fd](https://github.com/mcsrobert/ha-git-sync/commit/389d5fdd0b735710acda1b1b7f8728d227484794))
* **hook:** retry notifications without repeating the reload or restart ([14de7b3](https://github.com/mcsrobert/ha-git-sync/commit/14de7b3c7d904e6dd18d2f7de3c877ba1864bf96))
