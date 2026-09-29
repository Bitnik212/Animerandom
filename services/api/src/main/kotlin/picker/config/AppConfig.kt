package picker.config

import java.net.URI

/** Typed configuration from environment variables (see the service README). */
data class AppConfig(
    val postgres: PostgresConfig,
    val redisUrl: String,
    val elasticsearchUrl: String,
    val recEngineUrl: String,
    val recEngineTimeoutMs: Long,
    val defaultLocale: String,
    val keycloak: KeycloakConfig,
    val authRateLimitIp: RateLimit,
    val authRateLimitEmail: RateLimit,
    val emailVerificationRequired: Boolean,
    val jwtLeewaySeconds: Long,
    val runMigrations: Boolean = true,
    val dbPoolSize: Int = 10,
) {
    companion object {
        fun fromEnv(env: Map<String, String> = System.getenv()): AppConfig {
            fun get(key: String, default: String) = env[key]?.takeIf { it.isNotBlank() } ?: default
            return AppConfig(
                postgres = PostgresConfig.fromUrl(get("POSTGRES_URL", "postgresql://api_svc:api@postgres:5432/anime")),
                redisUrl = get("REDIS_URL", "redis://redis:6379/0"),
                elasticsearchUrl = get("ELASTICSEARCH_URL", "http://elasticsearch:9200"),
                recEngineUrl = get("REC_ENGINE_URL", "http://rec-engine:8081"),
                recEngineTimeoutMs = get("REC_ENGINE_TIMEOUT_MS", "800").toLong(),
                defaultLocale = get("DEFAULT_LOCALE", "en"),
                keycloak =
                    KeycloakConfig(
                        internalUrl = get("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080").trimEnd('/'),
                        issuer = get("KEYCLOAK_ISSUER", "http://localhost:8180/realms/anime-picker"),
                        realm = get("KEYCLOAK_REALM", "anime-picker"),
                        clientId = get("KEYCLOAK_CLIENT_ID", "anime-picker-api"),
                        clientSecret = get("KEYCLOAK_CLIENT_SECRET", ""),
                    ),
                authRateLimitIp = RateLimit.parse(get("AUTH_RATE_LIMIT_IP", "20/min")),
                authRateLimitEmail = RateLimit.parse(get("AUTH_RATE_LIMIT_EMAIL", "5/min")),
                emailVerificationRequired = get("EMAIL_VERIFICATION_REQUIRED", "false").toBoolean(),
                jwtLeewaySeconds = get("JWT_LEEWAY_SECONDS", "30").toLong(),
                runMigrations = get("RUN_MIGRATIONS", "true").toBoolean(),
                dbPoolSize = get("DB_POOL_SIZE", "10").toInt(),
            )
        }
    }
}

data class PostgresConfig(val jdbcUrl: String, val user: String, val password: String) {
    companion object {
        /** `postgresql://user:password@host:port/db` → JDBC URL plus credentials. */
        fun fromUrl(url: String): PostgresConfig {
            val uri = URI(url.replaceFirst("postgres://", "postgresql://"))
            val (user, password) =
                (uri.rawUserInfo ?: "").split(":", limit = 2).let {
                    it.getOrElse(0) { "" } to it.getOrElse(1) { "" }
                }
            val port = if (uri.port == -1) 5432 else uri.port
            val query = uri.rawQuery?.let { "?$it" } ?: ""
            return PostgresConfig("jdbc:postgresql://${uri.host}:$port${uri.rawPath}$query", user, password)
        }
    }
}

data class KeycloakConfig(
    val internalUrl: String,
    val issuer: String,
    val realm: String,
    val clientId: String,
    val clientSecret: String,
) {
    val realmUrl get() = "$internalUrl/realms/$realm"
    val certsUrl get() = "$realmUrl/protocol/openid-connect/certs"
    val tokenUrl get() = "$realmUrl/protocol/openid-connect/token"
    val logoutUrl get() = "$realmUrl/protocol/openid-connect/logout"
    val adminUrl get() = "$internalUrl/admin/realms/$realm"

    /** Never prints the client secret. */
    override fun toString() =
        "KeycloakConfig(internalUrl=$internalUrl, issuer=$issuer, realm=$realm, clientId=$clientId)"
}

/** "20/min" style limits: [count] requests per [windowSeconds]. */
data class RateLimit(val count: Int, val windowSeconds: Long) {
    companion object {
        private val UNITS = mapOf("s" to 1L, "sec" to 1L, "min" to 60L, "m" to 60L, "h" to 3600L, "hour" to 3600L)

        fun parse(value: String): RateLimit {
            val (count, unit) = value.trim().split("/", limit = 2)
            val seconds = UNITS[unit.trim()] ?: throw IllegalArgumentException("Unknown rate unit in '$value'")
            return RateLimit(count.trim().toInt(), seconds)
        }
    }
}
