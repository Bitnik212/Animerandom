package picker.features.auth

import kotlinx.serialization.Serializable
import picker.auth.KeycloakClient
import picker.auth.RateLimiter
import picker.auth.UserPrincipal
import picker.config.AppConfig
import picker.errors.ApiException
import picker.errors.Errors
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import picker.infra.redis.deleteUserKeys
import java.util.UUID

@Serializable
data class ChangePasswordRequest(val currentPassword: String, val newPassword: String)

@Serializable
data class AdminUserView(
    val id: String,
    val email: String?,
    val enabled: Boolean,
    val emailVerified: Boolean,
    val roles: List<String>,
    val createdTimestamp: Long?,
    val listSize: Long,
    val ratings: Long,
)

/** Account management that needs Keycloak's Admin API (README "Account management"). */
class AccountService(
    private val config: AppConfig,
    private val keycloak: KeycloakClient,
    private val users: UserRepository,
    private val redis: Redis,
    private val limiter: RateLimiter,
) {
    /** Verifies the current password with a password grant, then resets it. */
    suspend fun changePassword(principal: UserPrincipal, request: ChangePasswordRequest) {
        Validation.password(request.newPassword)
        val email = principal.email ?: keycloakEmail(principal.id)
        limiter.hit(Keys.authRateEmail(email), config.authRateLimitEmail)
        try {
            keycloak.passwordGrant(email, request.currentPassword)
        } catch (e: ApiException) {
            if (e.type == "invalid-credentials") throw Errors.invalidCredentials()
            throw e
        }
        keycloak.resetPassword(principal.id, request.newPassword)
    }

    /** Keeps Keycloak's `locale` attribute in step with app_user, so emails match the UI. */
    suspend fun syncLocale(userId: UUID, locale: AppLocale) = keycloak.setLocale(userId, locale.key)

    suspend fun logoutEverywhere(userId: UUID) = keycloak.logoutAllSessions(userId)

    /**
     * App rows in one transaction, then `user:{id}:*` in Redis, then the Keycloak user.
     * Every step is idempotent, so a client retries after a 502.
     */
    suspend fun deleteAccount(userId: UUID) {
        users.deleteAll(userId)
        redis.deleteUserKeys(userId)
        keycloak.deleteUser(userId)
    }

    suspend fun adminView(userId: UUID): AdminUserView {
        val user = keycloak.getUser(userId)
        val stats = users.stats(userId)

        fun str(key: String) = (user[key] as? kotlinx.serialization.json.JsonPrimitive)?.content
        return AdminUserView(
            id = userId.toString(),
            email = str("email"),
            enabled = str("enabled")?.toBoolean() ?: false,
            emailVerified = str("emailVerified")?.toBoolean() ?: false,
            roles = keycloak.realmRoles(userId).filter { it == "user" || it == UserPrincipal.ADMIN_ROLE },
            createdTimestamp = str("createdTimestamp")?.toLongOrNull(),
            listSize = stats.listSize,
            ratings = stats.ratings,
        )
    }

    suspend fun setAdmin(userId: UUID, granted: Boolean) =
        keycloak.setRealmRole(userId, UserPrincipal.ADMIN_ROLE, granted)

    private suspend fun keycloakEmail(id: UUID): String =
        (keycloak.getUser(id)["email"] as? kotlinx.serialization.json.JsonPrimitive)?.content
            ?: throw Errors.accountNotReady("The account has no email")
}
