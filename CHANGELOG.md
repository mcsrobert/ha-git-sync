# Changelog

## [0.4.0](https://github.com/mcsrobert/ha-git-sync/compare/v0.3.1...v0.4.0) (2026-11-24)


### ⚠ BREAKING CHANGES

* **hook:** the files a deploy links into /config, except custom_components and www, now carry the time of the switch instead of git-sync's checkout. Anything that compares those times with the commit should read the commit instead.
* **check:** with lovelace: resource_mode: yaml, check fails unless every card in hacs.yaml has a resource under lovelace: resources: at one of its files, ending in ?v=<version> with the version from hacs.yaml, and every /local/community resource belongs to such a card. Add or fix the resources it lists. Configurations with resources in the UI are not affected.

### Features

* **check:** check YAML dashboards and card resources ([7f7ba02](https://github.com/mcsrobert/ha-git-sync/commit/7f7ba022da62acfca92c88d277032dec62cd9841))
* **renovate:** update the ?v= of card resources with the card ([c5264b2](https://github.com/mcsrobert/ha-git-sync/commit/c5264b2ad896b67c4431e46470d0af1a61642bae))


### Bug Fixes

* **hook:** date each new deploy's files after the switch ([d1fd1f8](https://github.com/mcsrobert/ha-git-sync/commit/d1fd1f885eeaefbd8fe96f864ad49512997e348f))

## [0.3.1](https://github.com/mcsrobert/ha-git-sync/compare/v0.3.0...v0.3.1) (2026-11-23)


### Bug Fixes

* **ready:** start Home Assistant with the last deploy ([ace4350](https://github.com/mcsrobert/ha-git-sync/commit/ace4350093d33ddfd4cb73b698493e0bb6999403))

## [0.3.0](https://github.com/mcsrobert/ha-git-sync/compare/v0.2.1...v0.3.0) (2026-11-23)


### ⚠ BREAKING CHANGES

* **hook:** /config/custom_components is now a link to .git-sync/current/custom_components instead of a copy. The first deploy with this version replaces the copies earlier versions made, without restarting Home Assistant. The image is now based on python:3.14-slim with git, ssh and the git-sync binary, instead of the git-sync image. It runs as the same user with the same settings, but no longer contains the other tools of the git-sync image, and has no setuid or setgid programs, because /etc/passwd stays writable for GITSYNC_ADD_USER.

### Features

* **hook:** rewrite the sidecar in Python and switch each deploy at once ([ad84e5d](https://github.com/mcsrobert/ha-git-sync/commit/ad84e5da0382518d9c0dc91e7b054155f2651a43))


### Bug Fixes

* **deps:** update dependency python to v3.15.2 ([43dddab](https://github.com/mcsrobert/ha-git-sync/commit/43dddab917dbef54a566eff80268ee10a9db9303))

## [0.2.1](https://github.com/mcsrobert/ha-git-sync/compare/v0.2.0...v0.2.1) (2026-11-21)


### Bug Fixes

* **check:** report the downloaded asset's hash on a digest mismatch ([631c29a](https://github.com/mcsrobert/ha-git-sync/commit/631c29a7fccd61f44b35e3773c143ed6ce325b14))
* **renovate:** update quoted sha256 and commit pins in hacs.yaml ([ddde80e](https://github.com/mcsrobert/ha-git-sync/commit/ddde80ec804493d4f1c6f6eb9ce5b8fd55b60d36))

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
