# KCIDB cloud deployment - password management
#
if [ -z "${_PASSWORD_SH+set}" ]; then
declare _PASSWORD_SH=

. secret.sh

# A map of password names and their descriptions
declare -r -A PASSWORD_DESCS=(
    [smtp]="SMTP"
    [psql_superuser]="PostgreSQL superuser"
    [psql_submitter]="PostgreSQL submitter user"
    [psql_viewer]="PostgreSQL viewer user"
)

# A map of password names and their "can be auto-generated" flags.
# The corresponding password will be auto-generated if the flag is "true", and
# no source file nor secret was specified for it.
declare -A PASSWORD_GENERATE=(
    [psql_submitter]="true"
    [psql_viewer]="true"
)

# A map of password names and their project and secret names, separated by a
# colon. Used for retrieving passwords if they have no source files specified.
declare -A PASSWORD_SECRETS=()

# A map of password names and their source files
declare -A PASSWORD_FILES=()

# A map of password names and their strings
declare -A PASSWORD_STRINGS=()

# Ask the user to input a password with specified name.
# Args: name
# Output: The retrieved password
function password_input() {
    declare -r name="$1"; shift
    if ! [[ -v PASSWORD_DESCS[$name] ]]; then
        echo "Unknown password name ${name@Q}" >&2
        exit 1
    fi
    declare password
    read -p "Enter ${PASSWORD_DESCS[$name]:-a} password: " -r -s password
    echo "" >&2
    echo -n "$password"
}

# Get a password with specified name, either from the cache, from its source
# file, from its secret, or from the user. Make sure the retrieved password is
# cached.
# Args: name
# Output: The retrieved password
function password_get() {
    declare -r name="$1"; shift
    if ! [[ -v PASSWORD_DESCS[$name] ]]; then
        echo "Unknown password name ${name@Q}" >&2
        exit 1
    fi

    declare password
    declare -r password_file="${PASSWORD_FILES[$name]:-}"
    declare -r password_secret="${PASSWORD_SECRETS[$name]:-}"
    declare password_secret_exists
    password_secret_exists=$(
        if [ -n "$password_secret" ]; then
            secret_exists "${password_secret%%:*}" "${password_secret#*:}"
        else
            echo "false"
        fi
    )
    declare -r password_secret_exists
    declare -r password_generate="${PASSWORD_GENERATE[$name]:-false}"

    # If cached
    if [[ -v PASSWORD_STRINGS[$name] ]]; then
        password="${PASSWORD_STRINGS[$name]}"
    # If file is specified
    elif [ -n "$password_file" ]; then
        # If asked to read from standard input
        if [ "$password_file" == "-" ]; then
            password=$(password_input "$name")
        else
            password=$(<"$password_file")
        fi
    # If secret exists
    elif "$password_secret_exists"; then
        password=$(
            secret_get "${password_secret%%:*}" "${password_secret#*:}"
        )
    # If can be generated
    elif "$password_generate"; then
        password=$(dd if=/dev/random bs=32 count=1 status=none | base64)
    # Else read from user
    else
        password=$(password_input "$name")
    fi

    PASSWORD_STRINGS[$name]="$password"

    echo -n "$password"
}

# Get the passwords with the specified names as a PostgreSQL's .pgpass file,
# generated with the corresponding specified usernames.
# Args: [name username]...
# Output: The generated .pgpass file
function password_get_pgpass() {
    declare -r -a escape_argv=(sed -e 's/[:\\]/\\&/g')
    declare name
    declare username

    while (($#)); do
        name="$1"; shift
        if ! [[ -v PASSWORD_DESCS[$name] ]]; then
            echo "Unknown password name ${name@Q}" >&2
            exit 1
        fi
        username="$1"; shift

        # Cache the password in the current shell
        password_get "$name" > /dev/null

        # Output the pgpass line
        echo -n "*:*:*:"
        echo -n "$username" | "${escape_argv[@]}"
        echo -n ":"
        password_get "$name" | "${escape_argv[@]}"
    done
}

# Set the source file for a password with specified name. The file will be
# used as the source of the password by password_get, if it wasn't already
# retrieved (and cached) before. Can be specified as "-" to have password
# requested from standard input.
# Args: name file
function password_set_file() {
    declare -r name="$1"; shift
    if ! [[ -v PASSWORD_DESCS[$name] ]]; then
        echo "Unknown password name ${name@Q}" >&2
        exit 1
    fi
    declare -r file="$1"; shift
    PASSWORD_FILES[$name]="$file"
}

# Set the project and the name of the secret storing the password with
# specified name. The password will be retrieved from the secret, if it wasn't
# cached, and if its source file wasn't specified.
# Args: name project secret
function password_set_secret() {
    declare -r name="$1"; shift
    declare -r project="$1"; shift
    declare -r secret="$1"; shift
    if ! [[ -v PASSWORD_DESCS[$name] ]]; then
        echo "Unknown password name ${name@Q}" >&2
        exit 1
    fi
    if [[ "$project" = *:* ]]; then
        echo "Invalid project name ${project@Q}" >&2
        exit 1
    fi
    PASSWORD_SECRETS[$name]="$project:$secret"
}

# Specify the single-word command returning exit status specifying if the
# password with specified name could be auto-generated or not.
# Args: name generate
function password_set_generate() {
    declare -r name="$1"; shift
    declare -r generate="$1"; shift
    if ! [[ -v PASSWORD_DESCS[$name] ]]; then
        echo "Unknown password name ${name@Q}" >&2
        exit 1
    fi
    PASSWORD_GENERATE[$name]="$generate"
}

# Check if any of the passwords with specified names are explicitly specified
# by the command-line user. That is, if any of them has a source file.
# Args: name...
function password_is_specified() {
    declare name
    while (($#)); do
        name="$1"; shift
        if ! [[ -v PASSWORD_DESCS[$name] ]]; then
            echo "Unknown password name ${name@Q}" >&2
            exit 1
        fi
        if ! [[ -v PASSWORD_FILES[$name] ]]; then
            return 1
        fi
    done
    return 0
}

# Deploy passwords to their secrets (assuming they're set with
# "password_set_secret"). For every password deploy only if the password is
# specified, or the secret doesn't exist.
# Args: name...
function password_deploy_secret() {
    declare name
    declare project
    declare secret
    declare exists
    while (($#)); do
        name="$1"; shift
        if ! [[ -v PASSWORD_DESCS[$name] ]]; then
            echo "Unknown password name ${name@Q}" >&2
            exit 1
        fi
        if ! [[ -v PASSWORD_SECRETS[$name] ]]; then
            echo "Password ${name@Q} has no secret specified" >&2
            exit 1
        fi
        project="${PASSWORD_SECRETS[$name]%%:*}"
        secret="${PASSWORD_SECRETS[$name]#*:}"
        exists=$(secret_exists "$project" "$secret")
        if ! "$exists" || password_is_specified "$name"; then
            # Get and cache the password in the current shell first
            password_get "$name" > /dev/null
            # Deploy the cached password
            password_get "$name" | secret_deploy "$project" "$name"
        fi
    done
}

# Withdraw passwords from their secrets (assuming they're set with
# "password_set_secret").
# Args: name...
function password_withdraw_secret() {
    declare name
    declare project
    declare secret
    while (($#)); do
        name="$1"; shift
        if ! [[ -v PASSWORD_DESCS[$name] ]]; then
            echo "Unknown password name ${name@Q}" >&2
            exit 1
        fi
        if ! [[ -v PASSWORD_SECRETS[$name] ]]; then
            echo "Password ${name@Q} has no secret specified" >&2
            exit 1
        fi
        project="${PASSWORD_SECRETS[$name]%%:*}"
        secret="${PASSWORD_SECRETS[$name]#*:}"
        secret_withdraw "$project" "$secret"
    done
}

# Deploy passwords (with corresponding user names) as a pgpass secret.
# Deploy only if one of the passwords is specified, or if the pgpass secret
# doesn't exist.
# Args: project pgpass_secret [password_name user_name]...
function password_deploy_pgpass_secret() {
    declare -r project="$1"; shift
    declare -r pgpass_secret="$1"; shift
    declare -a -r password_and_user_names=("$@")
    declare -a password_names
    while (($#)); do
        password_names+=("$1")
        shift 2
    done
    declare exists
    exists=$(secret_exists "$project" "$pgpass_secret")
    if ! "$exists" || password_is_specified "${password_names[@]}"; then
        # Cache the passwords in the current shell
        password_get_pgpass "${password_and_user_names[@]}" > /dev/null
        # Generate and deploy the .pgpass
        password_get_pgpass "${password_and_user_names[@]}" |
            secret_deploy "$project" "$pgpass_secret"
    fi
}

fi # _PASSWORD_SH