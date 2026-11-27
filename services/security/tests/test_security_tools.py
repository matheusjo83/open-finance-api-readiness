"""The PKI, the realm against FAPI-BR v2.2.0 and the conformance suite's plan configuration."""

import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from security_tools import pki, realm, suite
from security_tools.__main__ import ISSUER, RESOURCE_BASE, SUITE_BASE, render

SUITE = "https://conformance.security.svc"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("security")
    render(out)
    return out, json.loads((out / "pki/manifest.json").read_text())


def cert(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_every_certificate_is_signed_by_the_ca(built):
    out, manifest = built
    ca = cert(out / "pki/ca.pem")
    names = [s["cert"] for s in manifest["servers"].values()] + [c["cert"] for c in manifest["clients"].values()]
    for name in names:
        c = cert(out / "pki" / name)
        c.verify_directly_issued_by(ca)
        assert c.not_valid_after_utc > c.not_valid_before_utc


def test_server_certificates_name_the_in_cluster_services(built):
    out, manifest = built
    for server, entry in manifest["servers"].items():
        san = cert(out / "pki" / entry["cert"]).extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert f"{server}.security.svc" in san.value.get_values_for_type(x509.DNSName)


def test_client_certificates_carry_the_organisation_and_software_ids(built):
    out, manifest = built
    for entry in manifest["clients"].values():
        c = cert(out / "pki" / entry["cert"])
        assert c.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value == entry["organisation_id"]
        assert c.subject.get_attributes_for_oid(NameOID.USER_ID)[0].value == entry["software_id"]
        eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_the_two_clients_differ_in_every_key(built):
    # The suite's ensure-matching-key-in-authorization-request needs two different keys.
    out, manifest = built
    one, two = (json.loads((out / "pki" / c["jwks"]).read_text())["keys"] for c in manifest["clients"].values())
    assert {k["n"] for k in one}.isdisjoint({k["n"] for k in two})
    assert manifest["clients"]["client1"]["organisation_id"] != manifest["clients"]["client2"]["organisation_id"]


def test_public_jwks_have_no_private_part_and_match_the_private_ones(built):
    out, manifest = built
    for entry in manifest["clients"].values():
        private = json.loads((out / "pki" / entry["jwks"]).read_text())["keys"]
        public = entry["public_jwks"]["keys"]
        assert [{k: v for k, v in p.items() if k in ("kty", "kid", "use", "alg", "n", "e")} for p in private] == public
        assert all("d" not in k for k in public)
        assert {(k["use"], k["alg"]) for k in public} == {("sig", "PS256"), ("enc", "RSA-OAEP")}


def test_a_signing_key_signs_and_verifies(built):
    out, manifest = built
    entry = manifest["clients"]["client1"]
    key = json.loads((out / "pki" / entry["jwks"]).read_text())["keys"][0]

    def number(s):
        import base64
        return int.from_bytes(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), "big")

    from cryptography.hazmat.primitives.asymmetric import rsa
    pub = rsa.RSAPublicNumbers(number(key["e"]), number(key["n"]))
    priv = rsa.RSAPrivateNumbers(number(key["p"]), number(key["q"]), number(key["d"]), number(key["dp"]),
                                 number(key["dq"]), number(key["qi"]), pub).private_key()
    pss = padding.PSS(padding.MGF1(hashes.SHA256()), 32)
    pub.public_key().verify(priv.sign(b"x", pss, hashes.SHA256()), b"x", pss, hashes.SHA256())


def test_the_pki_is_kept_when_rendered_again(built, tmp_path):
    out, manifest = built
    before = (out / "pki/client1.pem").read_bytes()
    render(out)
    assert (out / "pki/client1.pem").read_bytes() == before


def realm_of(built):
    return realm.build(built[1], SUITE)


def test_every_client_follows_fapi_br(built):
    r = realm_of(built)
    assert len(r["clients"]) == 2
    for c in r["clients"]:
        a = c["attributes"]
        assert c["clientAuthenticatorType"] == "client-jwt"                  # private_key_jwt (5.1 item 2)
        assert a["require.pushed.authorization.requests"] == "true"         # PAR (5.1 item 3)
        assert a["pkce.code.challenge.method"] == "S256"                     # PKCE (5.1 item 18)
        assert a["tls.client.certificate.bound.access.tokens"] == "true"
        for key in ("token.endpoint.auth.signing.alg", "request.object.signature.alg",
                    "access.token.signed.response.alg", "id.token.signed.response.alg"):
            assert a[key] == "PS256"                                         # JWS: PS256 (6.2)
        assert (a["id.token.encrypted.response.alg"], a["id.token.encrypted.response.enc"]) == ("RSA-OAEP", "A256GCM")  # 5.1.1, 6.3
        assert json.loads(a["acr.loa.map"]) == {realm.LOA2: "1"}             # loa2 (5.1 item 6)
        assert c["implicitFlowEnabled"]                                      # code id_token (5.1 item 12)
        assert "consent" in c["optionalClientScopes"]
        assert all(u.startswith(f"{SUITE}/test/a/{realm.ALIAS}/callback") for u in c["redirectUris"])
        jwks = json.loads(a["jwks.string"])["keys"]
        assert jwks and all("d" not in k for k in jwks)


def test_the_realm_follows_fapi_br(built):
    r = realm_of(built)
    assert 300 <= r["accessTokenLifespan"] <= 900                            # 5.1 item 11
    assert int(r["attributes"]["parRequestUriLifespan"]) >= 120              # 5.1 item 16
    assert r["revokeRefreshToken"] is False                                  # no rotation (5.1 item 13)
    profile = r["clientProfiles"]["profiles"][0]
    assert {e["executor"] for e in profile["executors"]} == {"pkce-enforcer", "suppress-refresh-token-rotation"}
    policy = r["clientPolicies"]["policies"][0]
    assert policy["enabled"] and policy["profiles"] == ["fapi-1-advanced", profile["name"]]
    consent = next(s for s in r["clientScopes"] if s["name"] == "consent")
    assert consent["attributes"]["is.parameterized.scope"] == "true"         # consent:<id> (section 7)
    names = {s["name"] for s in r["clientScopes"]}
    assert {"basic", "acr", "profile"} <= names       # Keycloak's own defaults kept
    assert set(realm.PRODUCT_SCOPES) <= names                                # 5.1 item 23


def test_the_suite_plan_is_the_open_finance_one():
    assert suite.plan_name() == ("fapi1-advanced-final-test-plan[client_auth_type=private_key_jwt]"
                                 "[fapi_profile=openbanking_brazil][fapi_auth_request_method=pushed]"
                                 "[fapi_response_mode=plain_response]")


def test_the_suite_configuration_points_at_our_services(built):
    out, manifest = built
    plan = json.loads((out / "plan.json").read_text())
    assert plan["alias"] == realm.ALIAS
    assert plan["server"]["discoveryUrl"] == f"{ISSUER}/.well-known/openid-configuration"
    assert plan["resource"]["consentUrl"].startswith(RESOURCE_BASE)
    for client, mtls, name in (("client", "mtls", "client1"), ("client2", "mtls2", "client2")):
        entry = manifest["clients"][name]
        assert plan[client]["client_id"] == entry["client_id"]
        assert all("d" in k for k in plan[client]["jwks"]["keys"])
        assert plan[mtls]["cert"] == (out / "pki" / entry["cert"]).read_text()
    login = plan["browser"][0]
    assert login["match"].startswith(f"{ISSUER}/protocol/openid-connect/auth")
    assert login["tasks"][-1]["match"].startswith(f"{SUITE_BASE}/test/a/{realm.ALIAS}/callback")
    rejects = plan["override"]["fapi1-advanced-final-user-rejects-authentication"]["browser"][0]["tasks"]
    assert rejects[1]["commands"] == [["click", "name", "cancel"]]
    users = [t["commands"][0][3] for t in (login["tasks"][0], rejects[0])]
    assert users == list(realm.USERS)


def browser_log(*, filled=False, completed=True, at_callback=False):
    log = [{"result": "REVIEW", "src": "ExpectInvalidRequestUriErrorPage", "upload": "ph1"},
           {"src": "WebRunner", "msg": "Scripted browser HTTP response", "response_status_code": 302,
            "response_content": ""},
           {"src": "WebRunner", "msg": "Scripted browser HTTP response", "response_status_code": 400,
            "response_status_text": "Bad Request",
            "response_content": "<html><head><style>p{}</style><script>x()</script></head>"
                                "<body><h1>We are sorry...</h1><p>Invalid Request</p></body></html>"}]
    if completed:
        url = ("https://conformance.security.svc/test/a/ofar/callback#state=x" if at_callback
               else "https://keycloak.security.svc/realms/ofb/protocol/openid-connect/auth?client_id=c"
                    "&redirect_uri=https://conformance.security.svc/test/a/ofar/callback")
        log.append({"src": "WebRunner", "msg": "Completed processing of webpage", "url": url})
    if filled:
        log.append({"upload": "ph1", "img": "data:image/png;base64,AA=="})
    return log


def test_evidence_waits_for_the_browser_and_takes_its_last_response():
    from security_tools import evidence
    [(placeholder, response)] = evidence.pending(browser_log())
    assert placeholder == "ph1" and response["response_status_code"] == 400
    assert evidence.visible_text(response["response_content"]) == "We are sorry... Invalid Request"
    assert evidence.pending(browser_log(completed=False)) == []   # the browser is still on the page
    assert evidence.pending(browser_log(filled=True)) == []       # already filled
    # Back at the suite's callback: the server accepted the request, there is no error page.
    assert evidence.pending(browser_log(at_callback=True)) == []


def test_evidence_skips_a_first_visit_that_ended_at_the_callback():
    # Reuse of a request_uri: the first visit logs in and ends at the callback, the second ends
    # on the server's error page; the evidence is the second one's response.
    from security_tools import evidence
    log = browser_log(completed=False)
    log.insert(1, {"src": "WebRunner", "msg": "Completed processing of webpage",
                   "url": "https://conformance.security.svc/test/a/ofar/callback#state=x"})
    log.append({"src": "WebRunner", "msg": "Completed processing of webpage",
                "url": "https://keycloak.security.svc/realms/ofb/protocol/openid-connect/auth?x"})
    [(_, response)] = evidence.pending(log)
    assert response["response_status_code"] == 400


def test_evidence_is_a_png_that_says_it_is_not_a_screenshot():
    import base64, io
    from PIL import Image
    from security_tools import evidence
    uri = evidence.render([evidence.HEADER, "HTTP 400", "x" * 300])
    assert uri.startswith("data:image/png;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert image.format == "PNG" and image.height > 16 * 4   # the long line wrapped
    assert evidence.HEADER.startswith("Not a screenshot")


def test_results_take_the_plan_id():
    from security_tools import results
    assert results.plan_id("x\nCreated test plan, new id: ab12CD\nCreated test plan, new id: Zz9\n") == "Zz9"
    assert results.plan_id("nothing") is None


def record(module, status="FINISHED", result="PASSED", failures=(), warnings=()):
    return {"module": module, "status": status, "result": result,
            "failures": [{"condition": c} for c in failures], "warnings": [{"condition": c} for c in warnings]}


def test_outcomes_against_the_known_failures():
    from security_tools import results
    known = {"m": ({"A"}, "why")}
    assert results.outcome(record("x"), known) == "passed"
    assert results.outcome(record("x", result="SKIPPED"), known) == "skipped"
    assert results.outcome(record("x", result="REVIEW"), known) == "review"
    assert results.outcome(record("m", result="FAILED", failures=["A"]), known) == "known"
    assert results.outcome(record("m", status="INTERRUPTED", result="FAILED", failures=["A"]), known) == "known"
    assert results.outcome(record("m", result="WARNING", warnings=["A"]), known) == "known"
    # Another condition, another module, or nothing to show for it: unexpected.
    assert results.outcome(record("m", result="FAILED", failures=["A", "B"]), known) == "unexpected"
    assert results.outcome(record("x", result="FAILED", failures=["A"]), known) == "unexpected"
    assert results.outcome(record("m", status="INTERRUPTED", result=None), known) == "unexpected"
    assert results.outcome(record("m", result="REVIEW", failures=["A"]), known) == "known"
    # A warning in a module that passed does not count.
    assert results.outcome(record("x", warnings=["B"]), known) == "passed"


def test_summary_lists_what_breaks_the_gate():
    from security_tools import results
    rs = results.judge([record("a"), record("b", result="REVIEW"), record("c", result="FAILED", failures=["Z"])], {})
    s = results.summary(rs)
    assert s["unexpected"] == ["c"] and s["review"] == ["b"] and s["by_outcome"]["passed"] == 1


def test_every_known_failure_names_its_conditions_and_reason():
    from security_tools import suite
    assert len(suite.KNOWN) == 8
    for module, (conditions, reason) in suite.KNOWN.items():
        assert module.startswith("fapi1-advanced-final") and conditions and len(reason) > 40
