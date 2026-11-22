# syntax=docker/dockerfile:1
FROM registry.k8s.io/git-sync/git-sync:v4.8.0@sha256:a20644562fba94773e594885cb001aadb5a3172175f13ac1215e5b2033c9200e AS git-sync

FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + \
    && echo 'git-sync:x:65533:65533::/tmp:/sbin/nologin' >> /etc/passwd \
    && echo 'git-sync:x:65533:' >> /etc/group \
    && chmod 0666 /etc/passwd \
    && mkdir -p /ha-git-sync/ha_git_sync

COPY --from=git-sync /git-sync /git-sync
COPY --chmod=0555 image/hook image/ready /ha-git-sync/
COPY --chmod=0444 image/github_known_hosts /ha-git-sync/
COPY --chmod=0444 ha_git_sync/__init__.py ha_git_sync/errors.py ha_git_sync/layout.py ha_git_sync/sidecar.py /ha-git-sync/ha_git_sync/

ENV HOME=/tmp \
    GITSYNC_ROOT=/config/.git-sync/repo \
    GITSYNC_LINK=/config/.git-sync/incoming \
    GITSYNC_REF=deployed \
    GITSYNC_PERIOD=1m \
    GITSYNC_DEPTH=1 \
    GITSYNC_SUBMODULES=off \
    GITSYNC_MAX_FAILURES=-1 \
    GITSYNC_STALE_WORKTREE_TIMEOUT=10m \
    GITSYNC_EXECHOOK_COMMAND=/ha-git-sync/hook \
    GITSYNC_EXECHOOK_TIMEOUT=3m \
    GITSYNC_EXECHOOK_BACKOFF=30s \
    GITSYNC_SSH_KNOWN_HOSTS_FILE=/ha-git-sync/github_known_hosts

USER 65533:65533
WORKDIR /tmp
HEALTHCHECK --interval=30s --start-period=10m --start-interval=2s CMD ["/ha-git-sync/ready"]
ENTRYPOINT ["/git-sync"]

LABEL org.opencontainers.image.source=https://github.com/mcsrobert/ha-git-sync \
      org.opencontainers.image.licenses=Apache-2.0
