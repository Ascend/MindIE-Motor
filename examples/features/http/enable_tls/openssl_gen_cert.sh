#!/usr/bin/env bash

# Generate server certificate script
# Usage: ./gen_cert_openssl.sh <ca_path> <cert_path> [ca_password] [cert_password]
# Example: ./gen_cert_openssl.sh /path/to/ca /path/to/cert 1234qwer 5678asdf

set -e

# Check parameters
if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Error: Please provide CA certificate path and server certificate save path"
    echo "Usage: $0 <ca_path> <cert_path> [ca_password] [cert_password]"
    echo "Example: $0 /path/to/ca /path/to/cert 1234qwer 5678asdf"
    exit 1
fi

ca_path=$1
cert_path=$2
ca_pwd=${3:-1234qwer}
cert_pwd=${4:-5678asdf}

ca_key_file="${ca_path}/ca.key.pem"
ca_cert_file="${ca_path}/ca.pem"

# Check if CA files exist
if [ ! -f "$ca_key_file" ] || [ ! -f "$ca_cert_file" ]; then
    echo "Error: CA certificate or private key file not found"
    echo "CA key file: $ca_key_file"
    echo "CA cert file: $ca_cert_file"
    echo "Please generate CA certificate first using gen_ca_openssl.sh"
    exit 1
fi

# Create directory (if not exists)
cert_dir=$(dirname "$cert_path")
if [ ! -d "$cert_dir" ]; then
    mkdir -p "$cert_dir"
    chmod 700 "$cert_dir"
fi

cert_key_file="${cert_path}/cert.key.pem"
cert_key_decrypt_file="${cert_path}/decrypt.cert.key.pem"
cert_cert_file="${cert_path}/cert.pem"
cert_csr_file="${cert_path}/cert.csr"
cert_config_file="${cert_path}/cert.conf"

# Check if files already exist
if [ -f "$cert_key_file" ] || [ -f "$cert_cert_file" ] || [ -f "$cert_key_decrypt_file" ]; then
    echo "Warning: Server certificate or private key file already exists"
    read -p "Overwrite? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Operation cancelled"
        exit 1
    fi
fi

# Create OpenSSL configuration file
echo "Creating OpenSSL configuration file..."

# Build alt_names section dynamically from environment variables
alt_names_section="[alt_names]"
dns_count=1
ip_count=1

# Add localhost entries (always included)
alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = localhost"
dns_count=$((dns_count + 1))
alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = *.localhost"
dns_count=$((dns_count + 1))
alt_names_section="${alt_names_section}"$'\n'"IP.${ip_count} = 127.0.0.1"
ip_count=$((ip_count + 1))
alt_names_section="${alt_names_section}"$'\n'"IP.${ip_count} = ::1"
ip_count=$((ip_count + 1))

# Add CONTROLLER_SERVICE if set
if [ -n "$CONTROLLER_SERVICE" ]; then
    alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = ${CONTROLLER_SERVICE}"
    dns_count=$((dns_count + 1))
    echo "Added CONTROLLER_SERVICE: $CONTROLLER_SERVICE"
fi

# Add COORDINATOR_SERVICE if set
if [ -n "$COORDINATOR_SERVICE" ]; then
    alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = ${COORDINATOR_SERVICE}"
    dns_count=$((dns_count + 1))
    echo "Added COORDINATOR_SERVICE: $COORDINATOR_SERVICE"
fi

# Add COORDINATOR_INFER_SERVICE if set
if [ -n "$COORDINATOR_INFER_SERVICE" ]; then
    alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = ${COORDINATOR_INFER_SERVICE}"
    dns_count=$((dns_count + 1))
    echo "Added COORDINATOR_INFER_SERVICE: $COORDINATOR_INFER_SERVICE"
fi

# Add COORDINATOR_OBS_SERVICE if set
if [ -n "$COORDINATOR_OBS_SERVICE" ]; then
    alt_names_section="${alt_names_section}"$'\n'"DNS.${dns_count} = ${COORDINATOR_OBS_SERVICE}"
    dns_count=$((dns_count + 1))
    echo "Added COORDINATOR_OBS_SERVICE: $COORDINATOR_OBS_SERVICE"
fi

# Add POD_IP if set
if [ -n "$POD_IP" ]; then
    alt_names_section="${alt_names_section}"$'\n'"IP.${ip_count} = ${POD_IP}"
    ip_count=$((ip_count + 1))
    echo "Added POD_IP: $POD_IP"
fi

# Add HOST_IP if set
if [ -n "$HOST_IP" ]; then
    alt_names_section="${alt_names_section}"$'\n'"IP.${ip_count} = ${HOST_IP}"
    ip_count=$((ip_count + 1))
    echo "Added HOST_IP: $HOST_IP"
fi

# Write configuration file
cat > "$cert_config_file" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
C = CN
L = Shanghai
O = Huawei
OU = Ascend
CN = MindIE Server

[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName = @alt_names

${alt_names_section}
EOF
chmod 644 "$cert_config_file"

echo "Generating server private key (encrypted)..."
# Generate server private key (RSA 3072 bits, password encrypted)
openssl genrsa -aes256 -out "$cert_key_file" -passout pass:"$cert_pwd" 3072

echo "Generating server private key (decrypted)..."
# Generate decrypted version of the private key
openssl rsa -in "$cert_key_file" -out "$cert_key_decrypt_file" -passin pass:"$cert_pwd"

echo "Generating certificate signing request (CSR)..."
# Generate certificate signing request
openssl req -new -key "$cert_key_file" -out "$cert_csr_file" \
    -passin pass:"$cert_pwd" \
    -sha256 \
    -config "$cert_config_file"

echo "Generating server certificate..."
# Sign the certificate with CA
openssl x509 -req -days 365 -in "$cert_csr_file" -CA "$ca_cert_file" -CAkey "$ca_key_file" \
    -out "$cert_cert_file" \
    -passin pass:"$ca_pwd" \
    -CAcreateserial \
    -sha256 \
    -extensions v3_req \
    -extfile "$cert_config_file"

# Remove CSR file (no longer needed)
rm -f "$cert_csr_file"

# Set file permissions
chmod 600 "$cert_key_file"
chmod 600 "$cert_key_decrypt_file"
chmod 644 "$cert_cert_file"

echo "Server certificate generated successfully!"
echo "Server private key (encrypted): $cert_key_file"
echo "Server private key (decrypted): $cert_key_decrypt_file"
echo "Server certificate: $cert_cert_file"
echo "OpenSSL configuration file: $cert_config_file"
echo "CA password: $ca_pwd"
echo "Certificate password: $cert_pwd"
