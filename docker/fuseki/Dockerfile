# Fuseki for browl: the admin protocol on, the web UI off (DESIGN.md §2.4).

FROM eclipse-temurin:21-jre AS downloads
ARG FUSEKI_VERSION=6.2.0
ARG FUSEKI_SHA512=ba65f5867d2d4741b2ed9e2af5a0d4fbb447909894ab2a0c6bc4dac8997f4fe339c87b13c48d45d054977769f0f8bf763ea346b1f7792d5cdc458041bd43a132
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# archive.apache.org keeps every release; the dlcdn mirrors keep only the latest.
RUN curl -fsSL -o /fuseki.tar.gz \
        "https://archive.apache.org/dist/jena/binaries/apache-jena-fuseki-${FUSEKI_VERSION}.tar.gz" \
    && echo "${FUSEKI_SHA512}  /fuseki.tar.gz" | sha512sum -c - \
    && tar -xzf /fuseki.tar.gz --strip-components=1 "apache-jena-fuseki-${FUSEKI_VERSION}/fuseki-server.jar"

FROM eclipse-temurin:21-jre
# curl is for the container health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
# The same UID as the app, which writes the stores Fuseki opens and deletes.
RUN useradd --uid 10001 --user-group --no-create-home fuseki \
    && mkdir -p /fuseki /opt/fuseki/no-ui \
    && chown fuseki:fuseki /fuseki
COPY --from=downloads /fuseki-server.jar /opt/fuseki/
COPY shiro.ini /opt/fuseki/

# FUSEKI_BASE holds the registered datasets' configuration and, under databases/, their TDB2 stores.
# JAVA_OPTIONS sets the heap; docker-compose.yml overrides it.
ENV FUSEKI_BASE=/fuseki JAVA_OPTIONS=-Xmx4g
USER fuseki
EXPOSE 3030
# FusekiServerCmd is the only server command with the admin protocol, which comes with the UI; an empty static-content
# directory (--base) leaves the UI out. tdb:unionDefaultGraph makes every dataset's default graph the union of its
# named graphs (one per ontology and import), which the admin protocol's dataset template can't set.
CMD ["sh", "-c", "exec java $JAVA_OPTIONS -Dtdb:unionDefaultGraph=true -cp /opt/fuseki/fuseki-server.jar org.apache.jena.fuseki.main.cmds.FusekiServerCmd --port=3030 --base=/opt/fuseki/no-ui --shiro=/opt/fuseki/shiro.ini"]
