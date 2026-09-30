package picker.features.auth

import kotlinx.serialization.Serializable
import org.slf4j.LoggerFactory
import picker.auth.KeycloakClient
import picker.auth.RateLimiter
import picker.auth.TokenResponse
import picker.auth.toResponse
import picker.config.AppConfig
import picker.errors.ApiException
import picker.errors.Errors
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.infra.redis.Keys

/** Sign-up response when the email must be verified before the first sign-in. */
@Serializable
data class VerificationRequired(val verificationRequired: Boolean = true)

@Serializable
data class SignUpRequest(
    val email: String,
    val password: String,
    val displayName: String? = null,
    val locale: String? = null,
)

@Serializable
data class SignInRequest(val email: String, val password: String)

@Serializable
data class RefreshRequest(val refreshToken: String)

@Serializable
data class ForgotPasswordRequest(val email: String)

/** Sign-up result: tokens, or `verificationRequired` when the realm wants email verification first. */
data class SignUpResult(val tokens: TokenResponse?, val verificationRequired: Boolean)

internal object Validation {
    private val EMAIL = Regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
    const val MIN_PASSWORD = 8
    const val MAX_PASSWORD = 256
    const val MAX_DISPLAY_NAME = 80

    fun email(value: String): String {
        val email = value.trim()
        if (email.length > 254 || !EMAIL.matches(email)) throw Errors.validation("email is not a valid address")
        return email.lowercase()
    }

    fun password(value: String) {
        if (value.length < MIN_PASSWORD) throw Errors.weakPassword("password needs at least $MIN_PASSWORD characters")
        if (value.length > MAX_PASSWORD) throw Errors.validation("password is too long")
    }

    fun displayName(value: String?): String? {
        val name = value?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        if (name.length > MAX_DISPLAY_NAME) throw Errors.validation("displayName is longer than $MAX_DISPLAY_NAME")
        return name
    }

    fun locale(tag: String?, default: AppLocale): AppLocale =
        if (tag.isNullOrBlank()) default else AppLocale.fromTag(tag) ?: throw Errors.unsupportedLocale(tag)
}

/** Sign-up, sign-in, refresh, sign-out and password reset (README "Authentication"). */
class AuthService(
    private val config: AppConfig,
    private val keycloak: KeycloakClient,
    private val users: UserRepository,
    private val limiter: RateLimiter,
    private val defaultLocale: AppLocale,
) {
    private val log = LoggerFactory.getLogger(AuthService::class.java)

    suspend fun signUp(request: SignUpRequest, ip: String): SignUpResult {
        val email = Validation.email(request.email)
        Validation.password(request.password)
        val displayName = Validation.displayName(request.displayName)
        val locale = Validation.locale(request.locale, defaultLocale)
        limiter.hit(Keys.authRateIp(ip), config.authRateLimitIp)

        val id = keycloak.createUser(email, request.password, locale.key)
        try {
            if (config.emailVerificationRequired) keycloak.sendVerifyEmail(id)
            users.create(id, displayName, locale.key)
        } catch (e: Exception) {
            // Never leave a half-created account behind.
            runCatching { keycloak.deleteUser(id) }.onFailure { log.error("Could not roll back Keycloak user {}", id) }
            throw e
        }
        if (config.emailVerificationRequired) return SignUpResult(null, true)
        return SignUpResult(keycloak.passwordGrant(email, request.password).toResponse(), false)
    }

    suspend fun signIn(request: SignInRequest, ip: String): TokenResponse {
        val email = request.email.trim().lowercase()
        limiter.hit(Keys.authRateIp(ip), config.authRateLimitIp)
        limiter.hit(Keys.authRateEmail(email), config.authRateLimitEmail)
        if (request.password.isEmpty()) throw Errors.invalidCredentials()
        return keycloak.passwordGrant(email, request.password).toResponse()
    }

    suspend fun refresh(request: RefreshRequest): TokenResponse {
        if (request.refreshToken.isBlank()) throw Errors.validation("refreshToken is required")
        return keycloak.refresh(request.refreshToken).toResponse()
    }

    suspend fun signOut(request: RefreshRequest) {
        if (request.refreshToken.isBlank()) throw Errors.validation("refreshToken is required")
        keycloak.logout(request.refreshToken)
    }

    /** Always succeeds from the caller's view, so it can't be used to probe for accounts. */
    suspend fun forgotPassword(request: ForgotPasswordRequest, ip: String) {
        val email = runCatching { Validation.email(request.email) }.getOrNull() ?: return
        limiter.hit(Keys.authRateIp(ip), config.authRateLimitIp)
        // Per email too, so nobody can flood one inbox from many addresses.
        limiter.hit(Keys.authRateEmail(email), config.authRateLimitEmail)
        try {
            keycloak.findUserIdByEmail(email)?.let { keycloak.executeActionsEmail(it, listOf("UPDATE_PASSWORD")) }
        } catch (e: ApiException) {
            log.warn("Password reset email not sent: {}", e.type)
        }
    }
}
