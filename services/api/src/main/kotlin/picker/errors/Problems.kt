package picker.errors

import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.install
import io.ktor.server.plugins.BadRequestException
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.response.header
import io.ktor.server.response.respondText
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import org.slf4j.LoggerFactory

/** RFC 7807 body. `title` and `detail` are English, for logs; clients localize by `type`. */
@Serializable
data class Problem(val type: String, val title: String, val status: Int, val detail: String = "")

/** A failure with a stable problem `type` (see the Errors table in the README). */
open class ApiException(
    val type: String,
    val status: HttpStatusCode,
    val title: String,
    detail: String = "",
    val headers: Map<String, String> = emptyMap(),
) : RuntimeException(detail.ifEmpty { title }) {
    val detail: String = detail
}

object Errors {
    fun validation(
        detail: String,
    ) = ApiException("validation-failed", HttpStatusCode.BadRequest, "Validation failed", detail)

    fun unauthorized(detail: String = "") =
        ApiException(
            "unauthorized",
            HttpStatusCode.Unauthorized,
            "Unauthorized",
            detail,
            mapOf(HttpHeaders.WWWAuthenticate to "Bearer"),
        )

    fun forbidden(detail: String = "") = ApiException("forbidden", HttpStatusCode.Forbidden, "Forbidden", detail)

    fun invalidCredentials() =
        ApiException(
            "invalid-credentials",
            HttpStatusCode.Unauthorized,
            "Invalid email or password",
        )

    fun weakPassword(detail: String = "") =
        ApiException(
            "weak-password",
            HttpStatusCode.BadRequest,
            "Weak password",
            detail,
        )

    fun emailTaken() = ApiException("email-taken", HttpStatusCode.Conflict, "Email already registered")

    fun accountNotReady(detail: String = "") =
        ApiException("account-not-ready", HttpStatusCode.Forbidden, "Account is not ready", detail)

    fun tooManyAttempts(retryAfterSeconds: Long) =
        ApiException(
            "too-many-attempts",
            HttpStatusCode.TooManyRequests,
            "Too many attempts",
            headers = mapOf(HttpHeaders.RetryAfter to retryAfterSeconds.coerceAtLeast(1).toString()),
        )

    fun userNotFound(detail: String = "") =
        ApiException(
            "user-not-found",
            HttpStatusCode.NotFound,
            "User not found",
            detail,
        )

    fun animeNotFound(id: Long) =
        ApiException("anime-not-found", HttpStatusCode.NotFound, "Anime not found", "No anime with id $id")

    fun unsupportedLocale(value: String) =
        ApiException(
            "unsupported-locale",
            HttpStatusCode.BadRequest,
            "Unsupported locale",
            "Unsupported locale '$value'; use en, ru, ja or ja-Latn",
        )

    fun identityProviderUnavailable(detail: String = "") =
        ApiException(
            "identity-provider-unavailable",
            HttpStatusCode.BadGateway,
            "Identity provider unavailable",
            detail,
        )
}

private val problemJson = ContentType("application", "problem+json")
private val json = Json { encodeDefaults = true }
private val log = LoggerFactory.getLogger("picker.errors")

suspend fun io.ktor.server.application.ApplicationCall.respondProblem(e: ApiException) {
    e.headers.forEach { (name, value) -> response.header(name, value) }
    respondText(
        json.encodeToString(Problem.serializer(), Problem(e.type, e.title, e.status.value, e.detail)),
        problemJson,
        e.status,
    )
}

fun Application.installProblems() {
    install(StatusPages) {
        exception<ApiException> { call, cause -> call.respondProblem(cause) }
        exception<BadRequestException> { call, cause ->
            call.respondProblem(Errors.validation(cause.cause?.message ?: cause.message ?: "Malformed request"))
        }
        exception<Throwable> { call, cause ->
            log.error("Unhandled error on {}", call.request.local.uri, cause)
            call.respondProblem(ApiException("internal", HttpStatusCode.InternalServerError, "Internal error"))
        }
        status(HttpStatusCode.NotFound) { call, _ ->
            call.respondProblem(ApiException("not-found", HttpStatusCode.NotFound, "Not found"))
        }
    }
}
