FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY gradlew settings.gradle.kts build.gradle.kts ./
COPY gradle gradle
# Dependencies first, so source changes don't re-download them.
RUN ./gradlew --no-daemon -q dependencies > /dev/null
COPY src src
RUN ./gradlew --no-daemon -q installDist -x test

FROM eclipse-temurin:21-jre
RUN useradd --system --uid 10003 api
COPY --from=build /src/build/install/api /app
USER api
EXPOSE 8080
CMD ["/app/bin/api"]
