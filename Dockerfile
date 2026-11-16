# The browl app: Python, Java 21 for the in-process OWLAPI (JPype), ROBOT, and Jena's TDB2 loader (DESIGN.md §2.1).

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie

# Pinned downloads, checked against checksums recorded here when the pins were chosen.
FROM ${PYTHON_IMAGE} AS downloads
ARG ROBOT_VERSION=v1.9.11
ARG ROBOT_SHA256=53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26
ARG JENA_VERSION=6.2.0
ARG JENA_SHA512=2b467d2ff940c207aea935e012ecbb0f0ab3e6bf59b3fc4b3fbc6cf9cea11f1428e97882ec078a5ffc57e114e0f5c88ea98c4575e687cc1dd232f99324b7b2a2
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /robot.jar "https://github.com/ontodev/robot/releases/download/${ROBOT_VERSION}/robot.jar" \
    && echo "${ROBOT_SHA256}  /robot.jar" | sha256sum -c -
# Maven Central: every release, served from a CDN.
RUN curl -fsSL -o /jena.tar.gz \
        "https://repo1.maven.org/maven2/org/apache/jena/apache-jena/${JENA_VERSION}/apache-jena-${JENA_VERSION}.tar.gz" \
    && echo "${JENA_SHA512}  /jena.tar.gz" | sha512sum -c - \
    && mkdir /jena \
    && tar -xzf /jena.tar.gz -C /jena --strip-components=1 \
    && rm -rf /jena/javadoc /jena/src-examples

# A virtualenv with the locked runtime dependencies, then browl installed from its wheel; the final image gets only the
# virtualenv. The dependencies are installed first, so their layer is reused when only browl's code changes.
FROM ${PYTHON_IMAGE} AS python-build
ENV POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=true
RUN pip install --no-cache-dir poetry==2.5.1
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root
COPY README.md LICENSE ./
COPY browl ./browl
RUN poetry build --format wheel \
    && .venv/bin/pip install --no-cache-dir --no-deps dist/*.whl

FROM ${PYTHON_IMAGE}
# Java 21 (Jena 6 needs it) from Debian, which backports its security fixes; curl is for the container health check;
# Jena's bulk loader for large ontologies (tdb2.xloader) needs jq.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /usr/lib/jvm/java-21
# One UID for the app and Fuseki: the app's loader writes the TDB2 stores that Fuseki then opens and deletes.
RUN useradd --uid 10001 --user-group --create-home browl \
    && mkdir /data /fuseki \
    && chown browl:browl /data /fuseki

COPY --from=downloads /robot.jar /opt/robot/robot.jar
COPY --from=downloads /jena /opt/jena
COPY --from=python-build /app/.venv /app/.venv

# JAVA_HOME is the link made above, since Debian names the JVM's directory after its own architecture names.
# ROBOT_JAR and JENA_HOME match the settings' defaults; set here as well so the image says where its tools are.
ENV JAVA_HOME=/usr/lib/jvm/java-21 \
    PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    ROBOT_JAR=/opt/robot/robot.jar \
    JENA_HOME=/opt/jena
WORKDIR /app
USER browl
EXPOSE 8000
# One worker process (DESIGN.md §2.1).
CMD ["uvicorn", "browl.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
