package picker.auth

import io.ktor.client.HttpClient
import io.ktor.client.request.delete
import io.ktor.client.request.forms.submitForm
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.Parameters
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.slf4j.LoggerFactory
import picker.config.KeycloakConfig
import picker.errors.ApiException
import picker.errors.Errors
import java.time.Clock
import java.time.Instant
import java.util.UUID

/** Keycloak's token response, passed through to clients in camelCase. */
@Serializable
data class TokenSet(
    @SerialName("access_token") val accessToken: String,
    @SerialName("expires_in") val expiresIn: Long,
    @SerialName("refresh_token") val refreshToken: String? = null,
    @SerialName("refresh_expires_in") val refreshExpiresIn: Long? = null,
    @SerialName("token_type") val tokenType: String = "Bearer",
)

@Serializable
data class TokenResponse(
    val accessToken: String,
    val expiresIn: Long,
    val refreshToken: String?,
    val refreshExpiresIn: Long?,
    val tokenType: String,
)

fun TokenSet.toResponse() = TokenResponse(accessToken, expiresIn, refreshToken, refreshExpiresIn, tokenType)

@Serializable
private data class OAuthError(
    val error: String = "",
    @SerialName("error_description") val description: String = "",
)

/**
 * The only code that talks to Keycloak (README "Authentication"): the token endpoint for
 * password, refresh and client-credentials grants plus logout, and the Admin REST API
 * through the `anime-picker-api` service account. Never on the normal request path.
 *
 * Passwords and tokens are never logged; the client secret never leaves this class.
 */
class KeycloakClient(
    private val config: KeycloakConfig,
    private val http: HttpClient,
    private val clock: Clock = Clock.systemUTC(),
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val log = LoggerFactory.getLogger(KeycloakClient::class.java)
    private val serviceTokenLock = Mutex()

    @Volatile
    private var serviceToken: Pair<String, Instant>? = null

    // --- Token endpoint ------------------------------------------------------------

    suspend fun passwordGrant(email: String, password: String): TokenSet =
        tokenCall(
            mapOf("grant_type" to "password", "username" to email, "password" to password, "scope" to "openid"),
        ) { error ->
            when {
                error.error == "invalid_grant" && error.description.contains("not fully set up", ignoreCase = true) ->
                    Errors.accountNotReady("Email not verified or required actions pending")
                error.error == "invalid_grant" && error.description.contains("disabled", ignoreCase = true) ->
                    Errors.accountNotReady("Account disabled")
                error.error == "invalid_grant" -> Errors.invalidCredentials()
                else -> null
            }
        }

    suspend fun refresh(refreshToken: String): TokenSet =
        tokenCall(
            mapOf("grant_type" to "refresh_token", "refresh_token" to refreshToken),
        ) { error ->
            if (error.error ==
                "invalid_grant"
            ) {
                Errors.unauthorized("Refresh token is invalid or expired")
            } else {
                null
            }
        }

    suspend fun logout(refreshToken: String) {
        val response =
            call {
                http.submitForm(
                    config.logoutUrl,
                    Parameters.build {
                        append("client_id", config.clientId)
                        append("client_secret", config.clientSecret)
                        append("refresh_token", refreshToken)
                    },
                )
            }
        // An already-invalid refresh token means the session is gone either way.
        if (!response.status.isSuccess() && response.status != HttpStatusCode.BadRequest) {
            throw unavailable("logout", response)
        }
    }

    private suspend fun tokenCall(
        params: Map<String, String>,
        mapError: (OAuthError) -> ApiException?,
    ): TokenSet {
        val response =
            call {
                http.submitForm(
                    config.tokenUrl,
                    Parameters.build {
                        append("client_id", config.clientId)
                        append("client_secret", config.clientSecret)
                        params.forEach { (k, v) -> append(k, v) }
                    },
                )
            }
        val body = response.bodyAsText()
        if (response.status.isSuccess()) return json.decodeFromString(TokenSet.serializer(), body)
        val error = runCatching { json.decodeFromString(OAuthError.serializer(), body) }.getOrDefault(OAuthError())
        throw mapError(error) ?: unavailable("token (${error.error})", response)
    }

    /** Client-credentials token of the service account, cached until 30 s before expiry. */
    private suspend fun adminToken(): String {
        serviceToken?.let { (token, expires) -> if (clock.instant().isBefore(expires)) return token }
        return serviceTokenLock.withLock {
            serviceToken?.let { (token, expires) -> if (clock.instant().isBefore(expires)) return@withLock token }
            val set = tokenCall(mapOf("grant_type" to "client_credentials")) { null }
            serviceToken = set.accessToken to clock.instant().plusSeconds((set.expiresIn - 30).coerceAtLeast(0))
            set.accessToken
        }
    }

    // --- Admin REST API --------------------------------------------------------------

    /** Creates an enabled user with a password; returns the new Keycloak user id. */
    suspend fun createUser(email: String, password: String, locale: String): UUID {
        val body =
            buildJsonObject {
                put("username", email)
                put("email", email)
                put("enabled", true)
                put("attributes", buildJsonObject { put("locale", buildJsonArray { add(JsonPrimitive(locale)) }) })
                put(
                    "credentials",
                    buildJsonArray {
                        add(
                            buildJsonObject {
                                put("type", "password")
                                put("value", password)
                                put("temporary", false)
                            },
                        )
                    },
                )
            }
        val response = admin { http.post("${config.adminUrl}/users") { jsonBody(it, body) } }
        val location = response.headers[HttpHeaders.Location]
        if (response.status != HttpStatusCode.Created || location == null) throw createUserError(response)
        return UUID.fromString(location.substringAfterLast('/'))
    }

    private suspend fun createUserError(response: HttpResponse): ApiException =
        when (response.status) {
            HttpStatusCode.Conflict -> Errors.emailTaken()
            HttpStatusCode.BadRequest ->
                if (isPasswordPolicy(response.bodyAsText())) {
                    Errors.weakPassword("The password doesn't meet the password policy")
                } else {
                    Errors.validation("Keycloak rejected the account")
                }
            else -> unavailable("create user", response)
        }

    suspend fun sendVerifyEmail(id: UUID) =
        expectOk("send verify email", admin { http.put("${config.adminUrl}/users/$id/send-verify-email") { auth(it) } })

    suspend fun findUserIdByEmail(email: String): UUID? {
        val response =
            admin {
                http.get("${config.adminUrl}/users") {
                    auth(it)
                    parameter("email", email)
                    parameter("exact", "true")
                }
            }
        expectOk("find user", response)
        return json
            .parseToJsonElement(response.bodyAsText())
            .jsonArray
            .firstOrNull()
            ?.jsonObject
            ?.get("id")
            ?.jsonPrimitive
            ?.content
            ?.let(UUID::fromString)
    }

    suspend fun executeActionsEmail(id: UUID, actions: List<String>) {
        val body = JsonArray(actions.map(::JsonPrimitive))
        expectOk(
            "execute actions email",
            admin { http.put("${config.adminUrl}/users/$id/execute-actions-email") { jsonBody(it, body) } },
        )
    }

    suspend fun resetPassword(id: UUID, password: String) {
        val body =
            buildJsonObject {
                put("type", "password")
                put("value", password)
                put("temporary", false)
            }
        val response = admin { http.put("${config.adminUrl}/users/$id/reset-password") { jsonBody(it, body) } }
        if (response.status == HttpStatusCode.BadRequest && isPasswordPolicy(response.bodyAsText())) {
            throw Errors.weakPassword("The password doesn't meet the password policy")
        }
        expectOk("reset password", response)
    }

    /** The full user representation (Keycloak's PUT replaces it as a whole). */
    suspend fun getUser(id: UUID): JsonObject {
        val response = admin { http.get("${config.adminUrl}/users/$id") { auth(it) } }
        if (response.status == HttpStatusCode.NotFound) throw Errors.userNotFound("No Keycloak user $id")
        expectOk("get user", response)
        return json.parseToJsonElement(response.bodyAsText()).jsonObject
    }

    /** Read-modify-write of `attributes.locale`, keeping every other attribute. */
    suspend fun setLocale(id: UUID, locale: String) {
        val user = getUser(id)
        val attributes =
            (user["attributes"] as? JsonObject).orEmpty() + ("locale" to JsonArray(listOf(JsonPrimitive(locale))))
        val updated = JsonObject(user + ("attributes" to JsonObject(attributes)))
        expectOk("update user", admin { http.put("${config.adminUrl}/users/$id") { jsonBody(it, updated) } })
    }

    suspend fun logoutAllSessions(id: UUID) =
        expectOk("logout user", admin { http.post("${config.adminUrl}/users/$id/logout") { auth(it) } })

    /** Idempotent: a user that's already gone counts as deleted. */
    suspend fun deleteUser(id: UUID) {
        val response = admin { http.delete("${config.adminUrl}/users/$id") { auth(it) } }
        if (response.status != HttpStatusCode.NotFound) expectOk("delete user", response)
    }

    /** Effective realm roles, the same set a token carries (`user` comes through the default-roles composite). */
    suspend fun realmRoles(id: UUID): List<String> {
        val response = admin { http.get("${config.adminUrl}/users/$id/role-mappings/realm/composite") { auth(it) } }
        if (response.status == HttpStatusCode.NotFound) throw Errors.userNotFound("No Keycloak user $id")
        expectOk("get roles", response)
        return json
            .parseToJsonElement(
                response.bodyAsText(),
            ).jsonArray
            .map { it.jsonObject["name"]!!.jsonPrimitive.content }
    }

    /**
     * Grants or revokes a realm role. The role is looked up in the user's own mappings
     * (`available` to grant, direct to revoke): reading `/roles/{name}` would need `view-realm`,
     * which the service account doesn't have. Already in the wanted state → nothing to do.
     */
    suspend fun setRealmRole(id: UUID, role: String, granted: Boolean) {
        val url = "${config.adminUrl}/users/$id/role-mappings/realm"
        val lookup = admin { http.get(if (granted) "$url/available" else url) { auth(it) } }
        if (lookup.status == HttpStatusCode.NotFound) throw Errors.userNotFound("No Keycloak user $id")
        expectOk("get roles", lookup)
        val representation =
            json
                .parseToJsonElement(lookup.bodyAsText())
                .jsonArray
                .firstOrNull { it.jsonObject["name"]?.jsonPrimitive?.content == role }
                ?: return
        val body = JsonArray(listOf(representation))
        val response =
            admin {
                if (granted) http.post(url) { jsonBody(it, body) } else http.delete(url) { jsonBody(it, body) }
            }
        if (response.status == HttpStatusCode.NotFound) throw Errors.userNotFound("No Keycloak user $id")
        expectOk("set role", response)
    }

    // --- Plumbing ------------------------------------------------------------------

    private fun io.ktor.client.request.HttpRequestBuilder.auth(token: String) =
        header(HttpHeaders.Authorization, "Bearer $token")

    private fun io.ktor.client.request.HttpRequestBuilder.jsonBody(token: String, body: Any) {
        auth(token)
        contentType(ContentType.Application.Json)
        setBody(body.toString())
    }

    /** Admin call with a cached service token; one retry with a fresh token on 401. */
    private suspend fun admin(request: suspend (String) -> HttpResponse): HttpResponse {
        val first = call { request(adminToken()) }
        if (first.status != HttpStatusCode.Unauthorized) return first
        serviceToken = null
        return call { request(adminToken()) }
    }

    private suspend fun call(request: suspend () -> HttpResponse): HttpResponse =
        try {
            request()
        } catch (e: ApiException) {
            throw e
        } catch (e: Exception) {
            log.warn("Keycloak unreachable: {}", e.javaClass.simpleName)
            throw Errors.identityProviderUnavailable("Keycloak unreachable")
        }

    private suspend fun expectOk(operation: String, response: HttpResponse) {
        if (!response.status.isSuccess()) throw unavailable(operation, response)
    }

    private fun unavailable(operation: String, response: HttpResponse): ApiException {
        log.warn("Keycloak {} failed: HTTP {}", operation, response.status.value)
        return Errors.identityProviderUnavailable("Keycloak $operation failed (HTTP ${response.status.value})")
    }

    private fun isPasswordPolicy(body: String) =
        body.contains("password", ignoreCase = true) || body.contains("policy", ignoreCase = true)
}
