/*
 * Keycloak script mapper (feature `scripts`) on the client scope `consent`: when Keycloak issues
 * a customer's access token for consent:<id>, the customer has logged in and accepted the grant
 * screen, so the consent is approved at the transmitter (its stand-in
 * POST /mock/consents/<id>/authorise), with the customer's CPF, which must be the consent's.
 * The approval belongs to the transmitter in Open Finance; without a Java extension of ours this
 * is where Keycloak can make it (decision 49). Client credentials tokens are left alone. Packed
 * into a provider jar by `security_tools render`; adds no claim.
 */
var LOG = Java.type("org.jboss.logging.Logger").getLogger("ofar.consent");
var TRANSMITTER = "http://transmitter.ofb.svc";

var match = /(?:^| )consent:(\S+)/.exec(token.getScope() || "");
if (match && user.getServiceAccountClientLink() == null) {
  var HttpClient = Java.type("java.net.http.HttpClient");
  var HttpRequest = Java.type("java.net.http.HttpRequest");
  var BodyPublishers = Java.type("java.net.http.HttpRequest.BodyPublishers");
  var BodyHandlers = Java.type("java.net.http.HttpResponse.BodyHandlers");
  var URI = Java.type("java.net.URI");
  var Duration = Java.type("java.time.Duration");
  var url = TRANSMITTER + "/mock/consents/" + encodeURIComponent(match[1]) + "/authorise";
  var body = JSON.stringify({cpf: user.getFirstAttribute("cpf")});
  try {
    var request = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(5))
      .header("content-type", "application/json").POST(BodyPublishers.ofString(body)).build();
    var response = HttpClient.newHttpClient().send(request, BodyHandlers.ofString());
    // 409: already AUTHORISED (a refresh), or no longer awaiting: the transmitter decides.
    LOG.infof("consent %s for %s: %s %s", match[1], user.getUsername(), response.statusCode(), response.body());
  } catch (e) {
    LOG.warnf("consent %s for %s: the transmitter could not be reached: %s", match[1], user.getUsername(), e);
  }
}
exports = null;
