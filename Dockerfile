# syntax=docker/dockerfile:1
FROM registry.k8s.io/git-sync/git-sync:v4.8.0@sha256:a20644562fba94773e594885cb001aadb5a3172175f13ac1215e5b2033c9200e

COPY --chmod=0555 image/hook image/ready /ha-git-sync/
COPY --chmod=0444 image/github_known_hosts /ha-git-sync/

ENV GITSYNC_ROOT=/config/.git-sync/repo \
    GITSYNC_LINK=/config/.git-sync/current \
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

HEALTHCHECK --interval=30s --start-period=10m --start-interval=2s CMD ["/ha-git-sync/ready"]

LABEL org.opencontainers.image.source=https://github.com/mcsrobert/ha-git-sync \
      org.opencontainers.image.licenses=Apache-2.0
