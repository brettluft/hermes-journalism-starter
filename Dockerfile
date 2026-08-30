FROM nousresearch/hermes-agent:latest

USER root

ENV DISCORD_REQUIRE_MENTION=false \
    DISCORD_AUTO_THREAD=false

COPY --chmod=0755 docker/cont-init.d/00-journalism-bootstrap /etc/cont-init.d/00-journalism-bootstrap
COPY config/config.template.yaml /opt/hermes/cli-config.yaml.example
COPY SOUL.md /opt/hermes/docker/SOUL.md

CMD ["gateway", "run"]
