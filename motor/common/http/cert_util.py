# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import datetime
import os
import ssl
import stat
from datetime import timezone
from ssl import Purpose, create_default_context

from OpenSSL import crypto
from cryptography import x509 as crypt_x509
from cryptography.x509.oid import ExtensionOID

from motor.common.http.password_utils import clear_passwd, PasswordDecryptor
from motor.common.logger import get_logger
from motor.config.tls_config import TLSConfig

CryptoX509 = crypto.X509

logger = get_logger(__name__)

CA_CERTS = "ca_cert"
TLS_CERT = "tls_cert"
TLS_KEY = "tls_key"
TLS_CRL = "tls_crl"
SSL_MUST_KEYS = [CA_CERTS, TLS_CERT, TLS_KEY]

MIN_RSA_LENGTH = 3072
RSA_SHA_256 = "sha256WithRSAEncryption"
RSA_SHA_384 = "sha384WithRSAEncryption"
RSA_SHA_512 = "sha512WithRSAEncryption"

# Common string constants to avoid duplicate magic literals
READ_BINARY_MODE = "rb"
UTF8_ENCODING = "utf-8"


def validate_certs_and_keys_modulus(server_crt: CryptoX509, server_key: CryptoX509) -> bool:
    """Validate certificate and private key modulus match"""
    try:
        cert_pub_key = server_crt.get_pubkey()
        cert_rsa_key = cert_pub_key.to_cryptography_key()
        cert_modulus = cert_rsa_key.public_numbers().n

        key_rsa_key = server_key.to_cryptography_key()
        key_modulus = key_rsa_key.public_key().public_numbers().n
        return cert_modulus == key_modulus
    except Exception as e:
        logger.error(f"Modulus validation failed: {e}")
        return False


def validate_cert_signature(cert: CryptoX509, ca_cert: CryptoX509) -> bool:
    """
    Validate that certificate is signed by CA certificate

    Args:
        cert: Certificate to validate
        ca_cert: CA certificate that should have signed the certificate

    Returns:
        True if certificate is signed by CA, False otherwise
    """
    try:
        # Convert to cryptography objects for signature validation
        cert_crypto = cert.to_cryptography()
        ca_cert_crypto = ca_cert.to_cryptography()

        # Verify certificate signature using verify_directly_issued_by
        try:
            cert_crypto.verify_directly_issued_by(ca_cert_crypto)
        except (crypt_x509.InvalidSignature, ValueError, TypeError) as e:
            logger.error(f"Certificate signature validation failed: certificate is not signed by the provided CA: {e}")
            return False

        # Verify issuer matches CA subject (additional check)
        cert_issuer = cert.get_issuer()
        ca_subject = ca_cert.get_subject()

        if cert_issuer.hash() != ca_subject.hash():
            logger.error("Certificate issuer does not match CA subject")
            return False

        return True
    except Exception as e:
        logger.error(f"Certificate signature validation failed: {e}")
        return False


def has_expired(cert: CryptoX509) -> bool:
    """
    Check if certificate is invalid (expired or not yet valid)

    Returns True if certificate is expired or not yet valid, False otherwise
    """
    try:
        before_time_str = cert.get_notBefore().decode("utf-8")
        before_time = datetime.datetime.strptime(before_time_str, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
        after_time_str = cert.get_notAfter().decode("utf-8")
        after_time = datetime.datetime.strptime(after_time_str, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
        current_time = datetime.datetime.now(timezone.utc)
        # Certificate is invalid if current time is before notBefore or after notAfter
        return before_time > current_time or current_time > after_time
    except Exception as e:
        logger.error(f"Certificate validity check failed: {e}")
        return True


def validate_server_certs(server_cert: CryptoX509) -> bool:
    """Validate server certificate integrity and security"""
    try:
        # 1. Check if certificate certificate version is X509v3
        if server_cert.get_version() != 2:
            logger.error("The cert does not use X509v3")
            return False

        decode_format = 'utf-8'

        # 2. Check signature algorithm
        pkey_algorithm = server_cert.get_signature_algorithm()
        pkey_algorithm = pkey_algorithm.decode(decode_format)
        if pkey_algorithm not in [RSA_SHA_256, RSA_SHA_384, RSA_SHA_512]:
            logger.error(
                f"Insecure encryption algorithm detected: {pkey_algorithm}, "
                f"only {RSA_SHA_256}, {RSA_SHA_384}, and {RSA_SHA_512} are allowed"
            )
            return False

        # 3. Check RSA key length
        pkey = server_cert.get_pubkey()
        key_algorithm_id = pkey.type()
        if key_algorithm_id == crypto.TYPE_RSA:
            rsa_key = pkey.to_cryptography_key()
            rsa_length = rsa_key.key_size
            if rsa_length < MIN_RSA_LENGTH:
                logger.error(f"Insecure RSA key length, required no less than: {MIN_RSA_LENGTH}")
                return False
        else:
            logger.error("Certificate public key must use RSA")
            return False

        # 4. Check certificate purpose
        check_key_cert_sign = False
        check_crl_sign = False
        check_ca_true = False

        try:
            crypto_cert = server_cert.to_cryptography()
            try:
                basic_constraints = crypto_cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
                if basic_constraints.value.ca:
                    check_ca_true = True
            except crypt_x509.ExtensionNotFound:
                pass

            try:
                key_usage = crypto_cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE)
                if key_usage.value.key_cert_sign:
                    check_key_cert_sign = True
                if key_usage.value.crl_sign:
                    check_crl_sign = True
            except crypt_x509.ExtensionNotFound:
                pass
        except Exception as e:
            logger.error(f"Failed to check certificate extensions: {e}")

        if check_key_cert_sign or check_crl_sign or check_ca_true:
            logger.debug(
                f"The cert is not End Entity cert with check_certificate_sign: "
                f"{check_key_cert_sign}, check_crl_sign: {check_crl_sign}, "
                f"check_ca_true: {check_ca_true}"
            )

        # 5. Check if certificate has expired
        if has_expired(server_cert):
            logger.error("Server cert expired")
            return False

        return True
    except Exception as e:
        logger.error(f"Server certificate validation failed: {e}")
        return False


def _check_directory_permissions(cur_path: str) -> None:
    """Check directory permissions"""
    cur_stat = os.stat(cur_path)
    cur_mode = stat.S_IMODE(cur_stat.st_mode)
    if cur_mode != 0o700:
        raise RuntimeError("The permission of ssl directory should be 700")


def _check_invalid_ssl_filesize(ssl_options: dict[str, str]) -> None:
    """Check SSL file size"""

    def check_size(path: str):
        size = os.path.getsize(path)
        if size > max_size:
            raise RuntimeError("SSL file should not exceed 10MB!")

    max_size = 10 * 1024 * 1024  # Maximum file size is 10MB
    for ssl_key in SSL_MUST_KEYS:
        check_size(ssl_options[ssl_key])


def _check_invalid_ssl_path(ssl_options: dict[str, str], required_keys: list | None = None) -> None:
    """Check SSL path validity"""

    def check_single(key: str, path: str):
        if not os.path.exists(path):
            raise RuntimeError(f"Enum {key} path is invalid: {path}")
        _check_directory_permissions(os.path.dirname(path))

    if not isinstance(ssl_options, dict):
        raise RuntimeError("ssl_options should be a dict!")

    keys_to_check = required_keys if required_keys is not None else SSL_MUST_KEYS
    for ssl_key in keys_to_check:
        if ssl_key not in ssl_options.keys():
            raise RuntimeError(f"{ssl_key} should be provided when ssl enable!")
        check_single(ssl_key, ssl_options[ssl_key])


class CertValidationUtil:
    """Certificate utility class"""

    @classmethod
    def validate_revoke_list(cls, crl_file_path: str) -> bool:
        """Validate CRL revocation list"""
        try:
            if not os.path.exists(crl_file_path):
                logger.error(f"CRL file does not exist: {crl_file_path}")
                return False

            with open(crl_file_path, 'rb') as ca_crl_file:
                ca_crl_data = ca_crl_file.read()
                crl_crypto = crypt_x509.load_pem_x509_crl(ca_crl_data)

            last_update_time = crl_crypto.last_update_utc
            next_update_time = crl_crypto.next_update_utc
            current_time = datetime.datetime.now(timezone.utc)

            # Check if CRL is not yet valid (current time < last_update)
            if current_time < last_update_time:
                logger.error("Current time is earlier than last update time of CRL")
                return False

            # Check if CRL is expired (current time >= next_update)
            if current_time >= next_update_time:
                logger.error("Current time is later than next update time of CRL")
                return False

            # Check if CRL list is empty
            revoked_certs = list(crl_crypto)
            if not revoked_certs:
                logger.warning("CRL list is empty")
                # Empty CRL list is not necessarily an error, just a warning
            return True

        except Exception as e:
            logger.error(f"CRL validation failed: {str(e)}")
            return False

    @classmethod
    def validate_ca_certs(cls, ca_crt_path: str) -> bool:
        """Validate CA certificate integrity and security"""
        try:
            with open(ca_crt_path, "rb") as ca_crt_file:
                ca_cert = crypto.load_certificate(crypto.FILETYPE_PEM, ca_crt_file.read())

            # 1. Check if certificate version is X509v3
            if ca_cert.get_version() != 2:
                logger.error(f"The CA: {os.path.basename(ca_crt_path)} does not use X509v3")
                return False

            decode_format = 'utf-8'

            # 2. Check CA flag and key usage
            check_ca_flag = False
            check_digital_signature_flag = False
            check_key_cert_sign = False
            check_crl_sign = False

            try:
                crypto_cert = ca_cert.to_cryptography()
                # Check basicConstraints extension
                try:
                    basic_constraints = crypto_cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
                    if basic_constraints.value.ca:
                        check_ca_flag = True
                except crypt_x509.ExtensionNotFound:
                    pass

                # Check keyUsage extension
                try:
                    key_usage = crypto_cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE)
                    if key_usage.value.digital_signature:
                        check_digital_signature_flag = True
                    if key_usage.value.key_cert_sign:
                        check_key_cert_sign = True
                    if key_usage.value.crl_sign:
                        check_crl_sign = True
                except crypt_x509.ExtensionNotFound:
                    pass
            except Exception as e:
                logger.error(f"Failed to check CA certificate extensions: {e}")

            # Validate basic constraints (CA flag)
            if not check_ca_flag:
                logger.error(f"The CA file {os.path.basename(ca_crt_path)} CA flag is not found in basic constraints")
                return False

            # Validate key usage
            if not check_digital_signature_flag:
                logger.error(f"The CA file {os.path.basename(ca_crt_path)} Digital Signature is not found in key usage")
                return False

            if not check_key_cert_sign:
                logger.error(f"The CA file {os.path.basename(ca_crt_path)} Certificate Sign is not found in key usage")
                return False

            if not check_crl_sign:
                logger.error(f"The CA file {os.path.basename(ca_crt_path)} CRL Sign is not found in key usage")
                return False

            # Validate signature algorithm
            pkey_algorithm = ca_cert.get_signature_algorithm().decode(decode_format)
            if pkey_algorithm not in [RSA_SHA_256, RSA_SHA_384, RSA_SHA_512]:
                logger.error(
                    f"CA {os.path.basename(ca_crt_path)} uses insecure encryption "
                    f"algorithm: {pkey_algorithm}, only {RSA_SHA_256}, {RSA_SHA_384}, "
                    f"and {RSA_SHA_512} are allowed"
                )
                return False

            # Check RSA key length
            pkey = ca_cert.get_pubkey()
            key_algorithm_id = pkey.type()
            if key_algorithm_id == crypto.TYPE_RSA:
                rsa_key = pkey.to_cryptography_key()
                rsa_length = rsa_key.key_size
                if rsa_length < MIN_RSA_LENGTH:
                    logger.error(
                        f"{os.path.basename(ca_crt_path)} insecure RSA key length, "
                        f"required no less than: {MIN_RSA_LENGTH}"
                    )
                    return False

            # Calculate fingerprint
            fingerprint = ca_cert.digest(pkey_algorithm).decode(decode_format)
            logger.debug(
                f"CA path: {os.path.basename(ca_crt_path)} pKeyAlgorithm: {pkey_algorithm} Fingerprint: {fingerprint}"
            )

            # Check if certificate has expired
            if has_expired(ca_cert):
                logger.error(f"CA path: {os.path.basename(ca_crt_path)} CA cert expired")
                return False

            return True
        except Exception as e:
            logger.error(f"CA certificate validation failed: {str(e)}")
            return False

    @classmethod
    def query_cert_info(cls, crt_path: str) -> dict:
        """Query certificate detailed information"""
        try:
            with open(crt_path, "rb") as file:
                cert_data = file.read()
            cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert_data)
            not_before = cert.get_notBefore().decode('utf-8')
            not_after = cert.get_notAfter().decode('utf-8')
            issuer = cert.get_issuer()
            issuer_msg = f'{issuer.CN}, {issuer.O}, {issuer.OU}, {issuer.L}, {issuer.ST}, {issuer.C}'
            subject = cert.get_subject()
            subject_msg = f'{subject.CN}, {subject.O}, {subject.OU}, {subject.L}, {subject.ST}, {subject.C}'
            serial_number = cert.get_serial_number()
            version = cert.get_version()
            return {
                'Not Before': not_before,
                'Not After': not_after,
                'Issuer': issuer_msg,
                'Subject': subject_msg,
                'Serial Number': serial_number,
                'Version': version,
            }
        except Exception as e:
            logger.error(f"Certificate info query failed: {str(e)}")
            return {}

    @classmethod
    def query_crl_info(cls, crl_file_path: str) -> list:
        """Query CRL detailed information"""
        try:
            with open(crl_file_path, "rb") as file:
                crl_data = file.read()
            crl = crypt_x509.load_pem_x509_crl(crl_data)
            revoked_certs = list(crl)
            data = []
            if revoked_certs:
                for cert in revoked_certs:
                    serial_number = cert.serial_number
                    revoked_reason = None
                    try:
                        reason_ext = cert.extensions.get_extension_for_oid(
                            crypt_x509.oid.CRLEntryExtensionOID.CRL_REASON
                        )
                        revoked_reason = reason_ext.value.reason.name if reason_ext.value.reason else None
                    except crypt_x509.ExtensionNotFound:
                        pass
                    revocation_date = cert.revocation_date_utc.strftime('%Y%m%d%H%M%SZ')
                    item = {
                        'Serial Number': str(serial_number),
                        'Revoked Reason': revoked_reason,
                        'Revocation Date': revocation_date,
                    }
                    data.append(item)
            else:
                logger.debug("No revoked certificates found in the CRL")
            return data
        except Exception as e:
            logger.error(f"CRL info query failed: {str(e)}")
            return []

    @classmethod
    def validate_cert_and_key(
        cls, server_crt_path: str, server_key_path: str, plain_text: bytes = None, ca_crt_path: str = None
    ) -> bool:
        """
        Validate certificate and private key integrity and matching

        Args:
            server_crt_path: Server certificate file path
            server_key_path: Server private key file path
            plain_text: Password for encrypted private key (optional)
            ca_crt_path: CA certificate file path for certificate chain validation (optional)

        Returns:
            True if validation passes, False otherwise
        """
        server_key = None
        try:
            # Load X509 certificate
            with open(server_crt_path, READ_BINARY_MODE) as f:
                cert_data = f.read()
                server_cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert_data)

            with open(server_key_path, READ_BINARY_MODE) as f:
                key_data = f.read()

                # Try to load private key, supports both encrypted and unencrypted formats
                try:
                    if not plain_text:
                        # Try to load unencrypted private key
                        server_key = crypto.load_privatekey(crypto.FILETYPE_PEM, key_data)
                        logger.debug("Loaded unencrypted private key")
                    else:
                        # Try to load encrypted private key with provided password
                        server_key = crypto.load_privatekey(crypto.FILETYPE_PEM, key_data, passphrase=plain_text)
                        logger.debug("Loaded encrypted private key")
                except crypto.Error as e:
                    # If loading fails, try another method only if password was provided
                    if plain_text:
                        try:
                            # If password exists but first load failed, try loading without password
                            # This handles cases where password is provided but key is actually unencrypted
                            server_key = crypto.load_privatekey(crypto.FILETYPE_PEM, key_data)
                            logger.warning("Loaded private key without password (password may be unnecessary)")
                        except crypto.Error:
                            logger.error(f"Failed to load private key with provided password: {e}")
                            return False
                    else:
                        # If no password provided and loading fails, it's an error
                        logger.error(f"Failed to load private key: {e}")
                        return False

            # Validate private key file
            if server_key is None:
                logger.error("Failed to load private key")
                return False

            # Validate server certificate
            if not validate_server_certs(server_cert):
                logger.error("Server certificate validation failed")
                return False

            # Validate if certificate and private key match
            if not validate_certs_and_keys_modulus(server_cert, server_key):
                logger.error("Certificate and private key modulus mismatch")
                return False

            # Validate certificate chain if CA certificate is provided
            if ca_crt_path:
                try:
                    with open(ca_crt_path, READ_BINARY_MODE) as f:
                        ca_cert_data = f.read()
                        ca_cert = crypto.load_certificate(crypto.FILETYPE_PEM, ca_cert_data)

                    if not validate_cert_signature(server_cert, ca_cert):
                        logger.error(
                            "Certificate chain validation failed: server certificate is not signed by the provided CA"
                        )
                        return False
                    logger.debug("Certificate chain validation passed")
                except Exception as e:
                    logger.error(f"Failed to validate certificate chain: {e}")
                    return False

            return True
        except Exception as e:
            logger.error(f"Certificate and key validation failed: {str(e)}")
            return False

    @classmethod
    def validate_ca_crl(cls, ca_path: str, crl_path: str) -> bool:
        """Validate CRL signature match with CA certificate"""
        try:
            if not os.path.exists(ca_path):
                logger.error(f"CA certificate file does not exist: {ca_path}")
                return False

            if not os.path.exists(crl_path):
                logger.error(f"CRL file does not exist: {crl_path}")
                return False

            with open(crl_path, 'rb') as crl_path_file:
                crl_data = crl_path_file.read()
                crl_crypto = crypt_x509.load_pem_x509_crl(crl_data)

            with open(ca_path, "rb") as ca_crt_file:
                ca_cert = crypto.load_certificate(crypto.FILETYPE_PEM, ca_crt_file.read())

            ca_cert_crypto = ca_cert.to_cryptography()
            ca_pub_key = ca_cert_crypto.public_key()

            # Verify CRL signature
            valid_signature = crl_crypto.is_signature_valid(ca_pub_key)
            if valid_signature:
                return True
            else:
                logger.error(f'CRL {os.path.basename(crl_path)} is not valid for CA {os.path.basename(ca_path)}')
                return False
        except Exception as e:
            logger.error(f"CRL signature validation failed: {str(e)}")
            return False


class CertUtil:
    """Coordinator certificate utility class"""

    @staticmethod
    def query_certificate_info(cert_file: str) -> dict:
        """
        Query certificate detailed information

        Args:
            cert_file: Certificate file path

        Returns:
            Certificate information dictionary
        """
        return CertValidationUtil.query_cert_info(cert_file)

    @staticmethod
    def query_crl_info(crl_file: str) -> list:
        """
        Query CRL detailed information

        Args:
            crl_file: CRL file path

        Returns:
            CRL information list
        """
        return CertValidationUtil.query_crl_info(crl_file)

    @staticmethod
    def validate_certificate_chain(
        ca_file: str, cert_file: str, key_file: str, crl_file: str = None, password: str | None = None
    ) -> bool:
        """
        Validate complete certificate chain

        Args:
            ca_file: CA certificate file path
            cert_file: Certificate file path
            key_file: Private key file path
            crl_file: CRL file path (optional)
            password: Private key password

        Returns:
            Validation result
        """
        try:
            # Validate CA certificate
            if not CertValidationUtil.validate_ca_certs(ca_file):
                logger.error("CA certificate validation failed")
                return False

            # Validate CRL (if provided)
            if crl_file and os.path.exists(crl_file):
                if not CertValidationUtil.validate_ca_crl(ca_file, crl_file):
                    logger.error("CRL validation failed")
                    return False

            # Validate certificate and private key with certificate chain validation
            password_bytes = password.encode('utf-8') if password else None
            if not CertValidationUtil.validate_cert_and_key(cert_file, key_file, password_bytes, ca_file):
                logger.error("Certificate and key validation failed")
                return False

            logger.debug("Certificate chain validation completed successfully")
            return True

        except Exception as e:
            logger.error(f"Certificate chain validation failed: {e}")
            return False

    @staticmethod
    def configure_tls13_only(context: ssl.SSLContext) -> None:
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.maximum_version = ssl.TLSVersion.TLSv1_3

            # TLS 1.3 password suites
            tls13_ciphersuites = [
                'TLS_AES_128_GCM_SHA256',
                'TLS_AES_256_GCM_SHA384',
                'TLS_CHACHA20_POLY1305_SHA256',
                'TLS_AES_128_CCM_SHA256',
            ]

            if hasattr(context, 'set_ciphersuites'):
                try:
                    context.set_ciphersuites(':'.join(tls13_ciphersuites))
                    logger.debug(f"TLS 1.3 cipher suites configured: {', '.join(tls13_ciphersuites)}")
                except ssl.SSLError as e:
                    logger.warning(f"Failed to set TLS 1.3 cipher suites: {e}, using default")
            else:
                logger.warning(
                    "set_ciphersuites() not available, TLS 1.3 cipher suites will use OpenSSL defaults. "
                    "TLS version is still restricted to 1.3 only."
                )
        except Exception as e:
            logger.error(f"Failed to configure TLS 1.3: {e}")
            raise

    @classmethod
    def construct_cert_context(cls, config: dict[str, str]) -> object | None:
        """
        Construct certificate context - strict certificate validation

        Args:
            config: Certificate configuration dictionary

        Returns:
            SSL context object
        """
        try:
            # Check SSL configuration - create local copy to avoid modifying global state
            required_keys = SSL_MUST_KEYS.copy()
            if TLS_CRL in config:
                required_keys.append(TLS_CRL)

            _check_invalid_ssl_path(config, required_keys)
            _check_invalid_ssl_filesize(config)

            # Get password
            password = config.get("tls_passwd", "").encode(UTF8_ENCODING)

            # Use complete CA certificate validation
            if not CertValidationUtil.validate_ca_certs(config[CA_CERTS]):
                raise RuntimeError("CA certificate validation failed")

            # Validate CRL (if provided)
            if TLS_CRL in config:
                if not CertValidationUtil.validate_ca_crl(config[CA_CERTS], config[TLS_CRL]):
                    raise RuntimeError("CRL validation failed")

            # Use complete certificate and private key validation with certificate chain validation
            if not CertValidationUtil.validate_cert_and_key(
                config[TLS_CERT], config[TLS_KEY], password, config[CA_CERTS]
            ):
                raise RuntimeError("Certificate and key validation failed")

            # Create SSL context
            context = create_default_context(Purpose.SERVER_AUTH)

            # Configure TLS 1.3 and password suites
            CertUtil.configure_tls13_only(context)

            context.load_verify_locations(cafile=config[CA_CERTS])
            context.load_cert_chain(
                certfile=config[TLS_CERT], keyfile=config[TLS_KEY], password=password.decode(UTF8_ENCODING)
            )

            # Dynamically add attributes, save certificate path information
            context.cert_file = config.get(TLS_CERT, "")
            context.key_file = config.get(TLS_KEY, "")
            context.ca_file = config.get(CA_CERTS, "")

            logger.debug("SSL certificate context constructed successfully with full validation")
            return context

        except Exception as e:
            logger.error(f"Failed to construct certificate context: {e}")
            return None

    @classmethod
    def create_ssl_context(cls, tls_config: TLSConfig, purpose: Purpose = Purpose.SERVER_AUTH) -> ssl.SSLContext | None:
        """
        Create SSL context - using strict validation

        Args:
            tls_config: Certificate file path
            purpose: Purpose of server authentication

        Returns:
            SSL context object
        """
        cert_file = tls_config.cert_file
        key_file = tls_config.key_file
        ca_file = tls_config.ca_file
        password_file = tls_config.passwd_file
        crl_file = tls_config.crl_file
        try:
            # Check if file paths are empty or non-existent
            if not cert_file or not key_file or not ca_file:
                logger.error("Certificate files cannot be empty")
                return None

            if not os.path.exists(cert_file) or not os.path.exists(key_file) or not os.path.exists(ca_file):
                logger.error(f"Certificate files do not exist: cert={cert_file}, key={key_file}, ca={ca_file}")
                return None

            # Create SSL context
            if purpose == Purpose.SERVER_AUTH:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

            # Validate cert and key
            password_bytes = None
            if password_file and os.path.exists(password_file):
                password = PasswordDecryptor.decrypt(password_file)
                # Use strict certificate and key validation with certificate chain validation
                password_bytes = password.encode('utf-8') if password else None
                if not CertValidationUtil.validate_cert_and_key(cert_file, key_file, password_bytes, ca_file):
                    clear_passwd(password_bytes)
                    logger.error("Certificate and key validation failed")
                    return None

            # Load certificate chain
            context.load_cert_chain(
                certfile=cert_file, keyfile=key_file, password=password_bytes if password_bytes else None
            )
            clear_passwd(password_bytes)

            # Use strict CA certificate validation
            if not CertValidationUtil.validate_ca_certs(ca_file):
                logger.error("CA certificate validation failed")
                return None

            # Load CA certificate
            context.load_verify_locations(cafile=ca_file)

            if crl_file and not CertValidationUtil.validate_ca_crl(ca_file, crl_file):
                logger.error("CRL validation failed")
                return None

            logger.debug("SSL context created successfully")
            return context

        except Exception as e:
            logger.error(f"Failed to create SSL context: {e}")
            return None

    @classmethod
    def create_ssl_context_no_client_cert(
        cls, cert_file: str, key_file: str, ca_file: str = "", password_file: str | None = None
    ) -> object | None:
        """
        Create SSL context without requiring client certificate verification

        Args:
            cert_file: Certificate file path
            key_file: Private key file path
            ca_file: CA certificate file path (optional, for client cert verification)
            password_file: Private key password file path

        Returns:
            SSL context object
        """
        try:
            # Check if file paths are empty or non-existent
            if not cert_file or not key_file:
                logger.error("Certificate files cannot be empty")
                return None

            if not os.path.exists(cert_file) or not os.path.exists(key_file):
                logger.error(f"Certificate files do not exist: cert={cert_file}, key={key_file}")
                return None

            # CA file is optional for this method (no client cert verification)
            # Save original ca_file for certificate chain validation
            ca_cert_path_for_validation = ca_file if (ca_file and os.path.exists(ca_file)) else None
            if ca_file and not os.path.exists(ca_file):
                logger.warning(f"CA certificate file does not exist: {ca_file}, continuing without CA verification")
                ca_file = None

            password = PasswordDecryptor.decrypt(password_file) if password_file else ""

            # Use strict certificate and key validation with certificate chain validation (if CA provided)
            password_bytes = password.encode('utf-8') if password else None
            if not CertValidationUtil.validate_cert_and_key(
                cert_file, key_file, password_bytes, ca_cert_path_for_validation
            ):
                clear_passwd(password_bytes)
                logger.error("Certificate and key validation failed")
                return None

            # Create SSL context (no client cert verification)
            context = create_default_context(Purpose.SERVER_AUTH)

            # Configure TLS 1.3 and password suites
            CertUtil.configure_tls13_only(context)

            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # Don't require client certificates

            # Load CA certificate if provided (for server cert validation)
            if ca_cert_path_for_validation:
                try:
                    context.load_verify_locations(cafile=ca_cert_path_for_validation)
                except Exception as e:
                    logger.warning(f"Failed to load CA certificate: {e}, continuing without CA verification")

            # Load certificate chain
            context.load_cert_chain(
                certfile=cert_file, keyfile=key_file, password=password_bytes if password_bytes else None
            )

            logger.debug("SSL context created successfully (no client cert verification)")
            return context

        except Exception as e:
            logger.error(f"Failed to create SSL context (no client cert): {e}")
            return None
