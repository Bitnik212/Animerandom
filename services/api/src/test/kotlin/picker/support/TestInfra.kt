package picker.support

import com.auth0.jwt.JWT
import com.auth0.jwt.algorithms.Algorithm
import com.github.tomakehurst.wiremock.WireMockServer
import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.core.WireMockConfiguration.options
import io.ktor.server.application.Application
import picker.config.AppConfig
import picker.config.KeycloakConfig
import picker.config.PostgresConfig
import picker.config.RateLimit
import java.io.File
import java.security.KeyPairGenerator
import java.security.interfaces.RSAPrivateKey
import java.security.interfaces.RSAPublicKey
import java.sql.DriverManager
import java.time.Instant
import java.util.Base64
import java.util.UUID

private fun env(key: String, default: String) = System.getenv(key)?.takeIf { it.isNotBlank() } ?: default

/**
 * Shared test infrastructure: one fresh Postgres database per test JVM (catalog contract
 * + fixtures + this service's Flyway migrations), Redis DB 14, and a WireMock server that
 * stands in for Keycloak's JWKS endpoint.
 *
 * Point API_TEST_POSTGRES_URL at a role that can create databases (default: local port
 * 5433) and API_TEST_REDIS_URL at a Redis you don't mind flushing DB 14 of.
 */
object TestInfra {
    private val adminUrl =
        PostgresConfig.fromUrl(
            env("API_TEST_POSTGRES_URL", "postgresql://postgres@127.0.0.1:5433/postgres"),
        )
    val redisUrl: String = env("API_TEST_REDIS_URL", "redis://127.0.0.1:6380/14")
    private val dbName = "api_test_${ProcessHandle.current().pid()}"
    private val contract = File("../ingest-worker/contract/catalog-schema.sql")

    val postgres: PostgresConfig by lazy {
        admin { it.createStatement().execute("DROP DATABASE IF EXISTS $dbName") }
        admin { it.createStatement().execute("CREATE DATABASE $dbName") }
        Runtime.getRuntime().addShutdownHook(
            Thread {
                runCatching {
                    admin {
                        it.createStatement().execute(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$dbName'",
                        )
                        it.createStatement().execute("DROP DATABASE IF EXISTS $dbName")
                    }
                }
            },
        )
        val config = adminUrl.copy(jdbcUrl = adminUrl.jdbcUrl.substringBeforeLast("/") + "/$dbName")
        DriverManager.getConnection(config.jdbcUrl, config.user, config.password).use { conn ->
            conn.createStatement().execute(contract.readText())
            conn.createStatement().execute(File("src/test/resources/fixtures/catalog.sql").readText())
        }
        config
    }

    private fun admin(block: (java.sql.Connection) -> Unit) =
        DriverManager.getConnection(adminUrl.jdbcUrl, adminUrl.user, adminUrl.password).use(block)

    /** Runs SQL against the test database (setup and assertions). */
    fun sql(statement: String): List<Map<String, Any?>> =
        DriverManager.getConnection(postgres.jdbcUrl, postgres.user, postgres.password).use { conn ->
            conn.createStatement().use { st ->
                if (!st.execute(statement)) return emptyList()
                val rs = st.resultSet
                val cols = (1..rs.metaData.columnCount).map { rs.metaData.getColumnLabel(it) }
                buildList { while (rs.next()) add(cols.associateWith { rs.getObject(it) }) }
            }
        }

    /** Clears everything this service writes between tests. */
    fun resetAppData() {
        runCatching { sql("TRUNCATE app.user_feedback, app.user_anime, app.app_user") }
    }
}

object FakeKeycloak {
    const val REALM = "anime-picker"
    const val CLIENT_ID = "anime-picker-api"
    const val ISSUER = "http://localhost:8180/realms/anime-picker"
    const val KID = "test-key"

    val server: WireMockServer by lazy {
        WireMockServer(options().dynamicPort()).also {
            it.start()
            Runtime.getRuntime().addShutdownHook(Thread { it.stop() })
        }
    }

    private val keys = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
    private val otherKeys = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
    val baseUrl: String get() = server.baseUrl()

    /** Serves the public key on the certs URL. Call before each test (WireMock resets). */
    fun stubJwks() {
        val pub = keys.public as RSAPublicKey
        val b64 = Base64.getUrlEncoder().withoutPadding()

        fun enc(bytes: ByteArray) = b64.encodeToString(bytes.dropWhile { it == 0.toByte() }.toByteArray())
        val jwk =
            """{"keys":[{"kid":"$KID","kty":"RSA","alg":"RS256","use":"sig",""" +
                """"n":"${enc(pub.modulus.toByteArray())}","e":"${enc(pub.publicExponent.toByteArray())}"}]}"""
        server.stubFor(
            get(urlEqualTo("/realms/$REALM/protocol/openid-connect/certs"))
                .willReturn(aResponse().withHeader("Content-Type", "application/json").withBody(jwk)),
        )
    }

    /** An access token as Keycloak would issue it; override claims to break one rule at a time. */
    fun token(
        sub: UUID = UUID.randomUUID(),
        roles: List<String> = listOf("user"),
        issuer: String = ISSUER,
        audience: List<String> = listOf(CLIENT_ID, "account"),
        azp: String = CLIENT_ID,
        typ: String = "Bearer",
        expiresIn: Long = 300,
        notBeforeOffset: Long = 0,
        kid: String = KID,
        signWithOtherKey: Boolean = false,
        email: String = "user@example.com",
    ): String {
        val now = Instant.now()
        val pair = if (signWithOtherKey) otherKeys else keys
        return JWT
            .create()
            .withKeyId(kid)
            .withIssuer(issuer)
            .withAudience(*audience.toTypedArray())
            .withSubject(sub.toString())
            .withClaim("azp", azp)
            .withClaim("typ", typ)
            .withClaim("email", email)
            .withClaim("realm_access", mapOf("roles" to roles))
            .withJWTId(UUID.randomUUID().toString())
            .withIssuedAt(now)
            .withNotBefore(now.plusSeconds(notBeforeOffset))
            .withExpiresAt(now.plusSeconds(expiresIn))
            .sign(Algorithm.RSA256(pair.public as RSAPublicKey, pair.private as RSAPrivateKey))
    }
}

fun testConfig(overrides: AppConfig.() -> AppConfig = { this }): AppConfig =
    AppConfig(
        postgres = TestInfra.postgres,
        redisUrl = TestInfra.redisUrl,
        elasticsearchUrl = FakeKeycloak.baseUrl + "/es",
        recEngineUrl = FakeKeycloak.baseUrl + "/rec",
        recEngineTimeoutMs = 800,
        defaultLocale = "en",
        keycloak =
            KeycloakConfig(
                internalUrl = FakeKeycloak.baseUrl,
                issuer = FakeKeycloak.ISSUER,
                realm = FakeKeycloak.REALM,
                clientId = FakeKeycloak.CLIENT_ID,
                clientSecret = "test-secret",
            ),
        authRateLimitIp = RateLimit(20, 60),
        authRateLimitEmail = RateLimit(5, 60),
        emailVerificationRequired = false,
        jwtLeewaySeconds = 30,
        dbPoolSize = 2,
    ).overrides()

/** Hook for tests that need extra routes next to the real ones. */
typealias AppSetup = Application.() -> Unit
