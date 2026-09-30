package picker.config

import io.kotest.matchers.shouldBe
import org.junit.jupiter.api.Test

class ConfigTest {
    @Test
    fun `postgres URLs become JDBC URLs and credentials`() {
        PostgresConfig.fromUrl("postgresql://api_svc:s3cr%40t@postgres:5432/anime") shouldBe
            PostgresConfig("jdbc:postgresql://postgres:5432/anime", "api_svc", "s3cr@t")
        // Encoded ':' stays in the password; '+' is a literal plus, not a space.
        PostgresConfig.fromUrl("postgresql://a%2Fb:p%3Aw+%25d@db/anime").let {
            it.user shouldBe "a/b"
            it.password shouldBe "p:w+%d"
        }
        PostgresConfig.fromUrl("postgres://u@db/anime").jdbcUrl shouldBe "jdbc:postgresql://db:5432/anime"
    }

    @Test
    fun `forwarded headers are untrusted unless configured`() {
        AppConfig.fromEnv(emptyMap()).trustForwardedHeaders shouldBe false
        AppConfig.fromEnv(mapOf("TRUST_FORWARDED_HEADERS" to "true")).trustForwardedHeaders shouldBe true
    }

    @Test
    fun `rate limits parse`() {
        RateLimit.parse("20/min") shouldBe RateLimit(20, 60)
        RateLimit.parse("5 / s") shouldBe RateLimit(5, 1)
    }

    @Test
    fun `the client secret never shows up in toString`() {
        val kc = AppConfig.fromEnv(mapOf("KEYCLOAK_CLIENT_SECRET" to "very-secret")).keycloak
        kc.clientSecret shouldBe "very-secret"
        kc.toString().contains("very-secret") shouldBe false
    }
}
