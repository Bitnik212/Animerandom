package picker.auth

import com.auth0.jwk.JwkProvider
import com.auth0.jwk.JwkProviderBuilder
import com.auth0.jwt.interfaces.Payload
import io.ktor.server.application.Application
import io.ktor.server.application.install
import io.ktor.server.auth.Authentication
import io.ktor.server.auth.jwt.jwt
import picker.config.KeycloakConfig
import picker.errors.Errors
import picker.errors.respondProblem
import java.net.URI
import java.util.UUID
import java.util.concurrent.TimeUnit

const val AUTH_USER = "user"

/**
 * Keycloak's public keys: up to 10 keys cached for 24 h, at most 10 fetches per minute.
 * An unknown `kid` misses the cache and triggers one refetch, which picks up key rotation.
 */
fun keycloakJwks(config: KeycloakConfig): JwkProvider =
    JwkProviderBuilder(URI(config.certsUrl).toURL())
        .cached(10, 24, TimeUnit.HOURS)
        .rateLimited(10, 1, TimeUnit.MINUTES)
        .build()

/**
 * The `user` JWT provider. A token passes only if it is RS256-signed by a known key and
 * `iss`, `aud`, `azp`, `typ`, `exp` and `nbf` all hold (README "Token validation").
 */
fun Application.installTokenValidation(config: KeycloakConfig, jwks: JwkProvider, leewaySeconds: Long) {
    install(Authentication) {
        jwt(AUTH_USER) {
            verifier(jwks, config.issuer) {
                withAudience(config.clientId)
                withClaim("azp", config.clientId)
                withClaim("typ", "Bearer")
                withClaimPresence("sub")
                acceptLeeway(leewaySeconds)
            }
            validate { credential -> principalOf(credential.payload) }
            challenge { _, _ ->
                val detail =
                    if (call.request.headers["Authorization"] ==
                        null
                    ) {
                        "Missing bearer token"
                    } else {
                        "Invalid token"
                    }
                call.respondProblem(Errors.unauthorized(detail))
            }
        }
    }
}

internal fun principalOf(payload: Payload): UserPrincipal? {
    val id = runCatching { UUID.fromString(payload.subject) }.getOrNull() ?: return null
    val realmAccess = payload.getClaim("realm_access").asMap()
    val roles = (realmAccess?.get("roles") as? List<*>)?.filterIsInstance<String>()?.toSet() ?: emptySet()
    return UserPrincipal(id, payload.getClaim("email").asString(), roles, payload.id)
}
