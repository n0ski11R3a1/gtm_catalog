import 'dart:convert';

import 'package:http/http.dart' as http;

class MobileApi {
  MobileApi(String baseUrl)
      : _baseUri = Uri.parse(baseUrl.endsWith('/')
            ? baseUrl.substring(0, baseUrl.length - 1)
            : baseUrl);

  final Uri _baseUri;

  Future<Map<String, dynamic>> login(String username, String password) async {
    return _post('/api/mobile/login', {
      'username': username,
      'password': password,
    });
  }

  Future<Map<String, dynamic>> fetchCatalog(String token) async {
    return _get('/api/mobile/catalog', token);
  }

  Future<List<dynamic>> syncOrders(
    String token,
    List<Map<String, dynamic>> orders,
  ) async {
    final response = await _post(
      '/api/mobile/sync',
      {'orders': orders},
      token: token,
    );
    return response['results'] as List<dynamic>? ?? const [];
  }

  Future<Map<String, dynamic>> _get(String path, String token) async {
    final response = await http
        .get(
          _baseUri.resolve(path),
          headers: {'Authorization': 'Bearer $token'},
        )
        .timeout(const Duration(seconds: 25));
    return _decode(response);
  }

  Future<Map<String, dynamic>> _post(
    String path,
    Map<String, dynamic> body, {
    String? token,
  }) async {
    final headers = <String, String>{'Content-Type': 'application/json'};
    if (token != null) headers['Authorization'] = 'Bearer $token';
    final response = await http
        .post(
          _baseUri.resolve(path),
          headers: headers,
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 25));
    return _decode(response);
  }

  Map<String, dynamic> _decode(http.Response response) {
    final decoded = response.body.isEmpty
        ? <String, dynamic>{}
        : jsonDecode(response.body) as Map<String, dynamic>;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw MobileApiException(
        decoded['error']?.toString() ?? 'Server request failed.',
        response.statusCode,
      );
    }
    return decoded;
  }
}

class MobileApiException implements Exception {
  const MobileApiException(this.message, this.statusCode);

  final String message;
  final int statusCode;

  @override
  String toString() => message;
}
