package picker.i18n

import io.ktor.http.HttpHeaders
import io.ktor.server.application.ApplicationCall
import io.ktor.server.response.header
import io.ktor.util.AttributeKey
import picker.auth.userOrNull
import picker.features.users.UserRepository

private val resolvedLocale = AttributeKey<AppLocale>("resolvedLocale")

/**
 * The request's locale (README "Locale resolution"), resolved once per call and echoed in
 * `Content-Language`. The saved locale is only looked up for signed-in users without a
 * `lang` parameter or a supported `Accept-Language`.
 */
suspend fun ApplicationCall.locale(users: UserRepository, default: AppLocale): AppLocale {
    attributes.getOrNull(resolvedLocale)?.let { return it }
    val lang = request.queryParameters["lang"]
    val accept = request.headers[HttpHeaders.AcceptLanguage]
    val saved =
        if (lang.isNullOrBlank() && LocaleResolver.fromAcceptLanguage(accept) == null) {
            userOrNull()?.let { users.locale(it.id) }
        } else {
            null
        }
    val locale = LocaleResolver.resolve(lang, accept, saved, default)
    attributes.put(resolvedLocale, locale)
    response.header(HttpHeaders.ContentLanguage, locale.tag)
    return locale
}
