FROM steamcmd/steamcmd:ubuntu-24

LABEL description="Project Zomboid dedicated server"

COPY --chmod=0755 entrypoint.sh /opt/entrypoint.sh

WORKDIR /opt/pzserver

ENTRYPOINT ["/opt/entrypoint.sh"]
