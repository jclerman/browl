# Fuseki for browl: the admin protocol on, the web UI off (DESIGN.md §2.4).

ARG DEBIAN_IMAGE=debian:trixie-20261104-slim

FROM ${DEBIAN_IMAGE} AS downloads
ARG FUSEKI_VERSION=6.2.0
ARG FUSEKI_SHA512=ba65f5867d2d4741b2ed9e2af5a0d4fbb447909894ab2a0c6bc4dac8997f4fe339c87b13c48d45d054977769f0f8bf763ea346b1f7792d5cdc458041bd43a132
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# Maven Central: every release, served from a CDN.
RUN curl -fsSL -o /fuseki.tar.gz \
        "https://repo1.maven.org/maven2/org/apache/jena/apache-jena-fuseki/${FUSEKI_VERSION}/apache-jena-fuseki-${FUSEKI_VERSION}.tar.gz" \
    && echo "${FUSEKI_SHA512}  /fuseki.tar.gz" | sha512sum -c - \
    && tar -xzf /fuseki.tar.gz --strip-components=1 "apache-jena-fuseki-${FUSEKI_VERSION}/fuseki-server.jar"

FROM ${DEBIAN_IMAGE}
# Java 21 (Jena 6 needs it) from Debian, which backports its security fixes; curl is for the container health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*
# The same UID as the app, which writes the stores Fuseki opens and deletes.
RUN useradd --uid 10001 --user-group --create-home fuseki \
    && mkdir -p /fuseki /opt/fuseki/no-ui \
    && chown fuseki:fuseki /fuseki
COPY --from=downloads /fuseki-server.jar /opt/fuseki/
COPY shiro.ini jetty.xml /opt/fuseki/

# FUSEKI_BASE holds the registered datasets' configuration and, under databases/, their TDB2 stores.
# The heap is set by docker-compose.yml, through JDK_JAVA_OPTIONS (which the java launcher reads itself).
ENV FUSEKI_BASE=/fuseki
USER fuseki
EXPOSE 3030
# FusekiServerCmd is the only server command with the admin protocol, which comes with the UI; an empty static-content
# directory (--base) leaves the UI out. On running out of heap, Fuseki exits so the container restarts, instead of
# carrying on unable to answer queries. jetty.xml sets the port, and a pooled output buffer instead of Fuseki 6.2.0's,
# which churns direct memory (apache/jena GH-4084, fixed after 6.2.0; S-6). Drop it once Jena has the fix.
CMD ["java", "-XX:+ExitOnOutOfMemoryError", "-cp", "/opt/fuseki/fuseki-server.jar", \
     "org.apache.jena.fuseki.main.cmds.FusekiServerCmd", \
     "--jetty=/opt/fuseki/jetty.xml", "--base=/opt/fuseki/no-ui", "--shiro=/opt/fuseki/shiro.ini"]
