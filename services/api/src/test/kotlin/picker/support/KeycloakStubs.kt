package picker.support

import com.github.tomakehurst.wiremock.client.MappingBuilder
import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.containing
import com.github.tomakehurst.wiremock.client.WireMock.delete
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.put
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.client.WireMock.urlPathEqualTo
import java.util.UUID

/** WireMock stand-ins for Keycloak's token endpoint and Admin REST API. */
object KeycloakStubs {
    private const val REALM = "/realms/${FakeKeycloak.REALM}/protocol/openid-connect"
    const val ADMIN = "/admin/realms/${FakeKeycloak.REALM}"
    const val TOKEN = "$REALM/token"
    const val LOGOUT = "$REALM/logout"

    private fun stub(mapping: MappingBuilder) = FakeKeycloak.server.stubFor(mapping)

    fun tokens(access: String = "access-token", refresh: String = "refresh-token") =
        """{"access_token":"$access","expires_in":300,"refresh_token":"$refresh","refresh_expires_in":2592000,""" +
            """"token_type":"Bearer","scope":"openid email profile"}"""

    fun serviceAccount() =
        stub(
            post(urlEqualTo(TOKEN))
                .withRequestBody(containing("grant_type=client_credentials"))
                .willReturn(json(200, """{"access_token":"service-token","expires_in":300,"token_type":"Bearer"}""")),
        )

    fun passwordGrant(status: Int = 200, body: String = tokens()) =
        stub(
            post(urlEqualTo(TOKEN)).withRequestBody(containing("grant_type=password")).willReturn(json(status, body)),
        )

    fun invalidGrant(description: String) =
        passwordGrant(401, """{"error":"invalid_grant","error_description":"$description"}""")

    fun refreshGrant(status: Int = 200, body: String = tokens("access-2", "refresh-2")) =
        stub(
            post(
                urlEqualTo(TOKEN),
            ).withRequestBody(containing("grant_type=refresh_token")).willReturn(json(status, body)),
        )

    fun logout(status: Int = 204) = stub(post(urlEqualTo(LOGOUT)).willReturn(aResponse().withStatus(status)))

    fun createUser(id: UUID) =
        stub(
            post(urlEqualTo("$ADMIN/users")).willReturn(
                aResponse().withStatus(201).withHeader("Location", "${FakeKeycloak.baseUrl}$ADMIN/users/$id"),
            ),
        )

    fun createUserFails(status: Int, body: String = "") =
        stub(post(urlEqualTo("$ADMIN/users")).willReturn(json(status, body)))

    fun findByEmail(email: String, id: UUID?) =
        stub(
            get(
                urlPathEqualTo("$ADMIN/users"),
            ).withQueryParam(
                "email",
                com.github.tomakehurst.wiremock.client.WireMock
                    .equalTo(email),
            ).willReturn(json(200, if (id == null) "[]" else """[{"id":"$id","email":"$email"}]""")),
        )

    fun user(id: UUID, email: String = "user@example.com", attributes: String = """{"locale":["en"]}""") =
        stub(
            get(urlEqualTo("$ADMIN/users/$id")).willReturn(
                json(
                    200,
                    """{"id":"$id","username":"$email","email":"$email","enabled":true,"emailVerified":true,""" +
                        """"createdTimestamp":1790000000000,"attributes":$attributes}""",
                ),
            ),
        )

    fun ok(method: String, path: String, status: Int = 204) =
        stub(
            when (method) {
                "PUT" -> put(urlEqualTo(path))
                "POST" -> post(urlEqualTo(path))
                "DELETE" -> delete(urlEqualTo(path))
                else -> get(urlEqualTo(path))
            }.willReturn(aResponse().withStatus(status)),
        )

    fun json(status: Int, body: String) =
        aResponse().withStatus(status).withHeader("Content-Type", "application/json").withBody(body)
}
