# Entrega de fase

## 1. Objetivo

Separar completamente el ciclo de vida de una empresa del de sus personas usuarias: una empresa es un espacio de trabajo, no una cuenta de acceso.

## 2. Alcance ejecutado

- El alta de empresa solo solicita sus datos de identificación y la configuración de su base operativa.
- Crear una empresa ya no pide nombre, email ni contraseña, ni crea automáticamente un usuario o una membresía.
- Las cuentas con credenciales se crean después desde `Superadmin > Usuarios de empresas` y se asignan explícitamente a una empresa y un rol operativo.
- La primera cuenta con rol `Administrador` puede quedar marcada como propietaria de la empresa para conservar las reglas de suspensión segura.
- La cuenta global de Anchi se identifica por el email de plataforma configurado y es la única identidad que puede usar el rol `superadmin`.
- El login de una persona usuaria exige una membresía activa y la lleva al contexto de su empresa; si tiene varias, debe elegir una.
- Un usuario de empresa no puede entrar en `/superadmin`, aunque manipule la URL o tenga un rol operativo de administrador.
- El rol `Superadmin` no se muestra ni se puede asignar desde la gestión de usuarios de una empresa; solo el dueño global conserva ese acceso.
- El acceso a `/superadmin` no se mezcla con una sesión tenant: el Superadmin se mantiene en el plano global.
- La provisión externa legacy deja de crear automáticamente usuarios al crear una empresa.

## 3. Diagnóstico previo

El formulario de empresas todavía pedía credenciales y `create_company` creaba un `MasterUser` administrador junto con su membresía. Además, el endpoint añadía una membresía silenciosa al Superadmin creador. Eso mezclaba el dueño de la plataforma con usuarios de empresa y hacía que la empresa pareciera tener un propietario antes de que se asignara una persona real.

## 4. Cambios realizados

- Eliminados del formulario y del endpoint de alta de empresas los campos de administrador y contraseña.
- Eliminada la creación automática de `MasterUser`, `CompanyMembership` y actor local durante el alta de empresa.
- Reforzado `create_company_user` para aceptar únicamente roles tenant y rechazar la identidad global de plataforma.
- Reservado el email configurado para el Superadmin y protegido también contra asignaciones accidentales.
- Añadida una comprobación central para que solo la identidad de plataforma configurada sea considerada Superadmin.
- Endurecidos `current_master_user`, `require_master_admin` y `require_superadmin` para exigir identidad global sin membresía tenant activa.
- Simplificado el login: la identidad global va al panel de plataforma; el resto debe tener una membresía activa.
- Actualizada la interfaz de empresas y usuarios para explicar la separación de responsabilidades.
- Filtrados los selectores de roles de empresa y endurecidos sus endpoints para no aceptar el rol global `Superadmin`.
- Actualizados los tests de provisión, propiedad, login y acceso al panel.

## 5. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/superadmin/service.py` | Separar empresa de identidad y proteger usuarios | Lógica de dominio |
| `backend/app/superadmin/routes.py` | Alta de empresa sin credenciales ni membresía implícita | Endpoint |
| `backend/app/master/service.py` | Identificar de forma única al dueño de plataforma | Autenticación |
| `backend/app/master/provisioning.py` | Evitar usuarios automáticos en provisión externa legacy | Provisión |
| `backend/app/auth/routes.py` | Separar login global y login tenant | Flujo de sesión |
| `backend/app/auth/dependencies.py` | Restringir paneles master a la identidad global | Autorización |
| `backend/app/templates/superadmin/companies.html` | Retirar campos de usuario y contraseña | Interfaz |
| `backend/app/templates/superadmin/users.html` | Aclarar que aquí se crean accesos de empresa | Interfaz |
| `backend/app/users/routes.py` | Ocultar y rechazar `Superadmin` en la gestión tenant | Autorización y endpoint |
| `backend/tests/test_account_lifecycle.py` | Adaptar alta de empresa sin usuario | Tests |
| `backend/tests/test_superadmin.py` | Cubrir empresa sin login y protección del dueño global | Tests |
| `backend/tests/test_multitenancy_auth.py` | Verificar que un usuario tenant no entra en Superadmin | Tests |

## 6. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Mantener la empresa sin credenciales | El tenant es un ámbito de datos y configuración | Convertir cada empresa en una cuenta de login |
| Crear usuarios desde una operación separada | Permite asignar rol y empresa de forma explícita | Crear un administrador implícito al provisionar |
| Reservar el email de plataforma | Evita que el dueño global se asigne como usuario tenant | Confiar solo en el valor `platform_role_key` |
| Exigir identidad global sin membresía para `/superadmin` | Evita cruces entre control de plataforma y operación tenant | Permitir el panel por rol operativo |

## 7. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `backend\\.venv\\Scripts\\python.exe -m compileall -q app tests` | OK |
| `APP_ENV=test backend\\.venv\\Scripts\\python.exe -m unittest -q tests.test_account_lifecycle tests.test_superadmin` | 8 tests OK |
| `APP_ENV=test backend\\.venv\\Scripts\\python.exe -m unittest -q tests.test_multitenancy_auth` | 4 tests OK |

## 8. Riesgos y observaciones pendientes

- Las empresas antiguas que ya tienen una membresía creada por el flujo anterior no se borran automáticamente; deben revisarse desde el panel y conservarse o desactivarse según corresponda.
- La cuenta de plataforma debe configurarse con `PLATFORM_ADMIN_EMAIL` y una contraseña segura en cada entorno real.
- El worker debe terminar la provisión antes de que se creen usuarios para esa empresa; la interfaz ya refleja el estado de provisión.

## 9. Estado final de Git

- No se ha hecho commit ni push en esta fase.
- Se han conservado los cambios previos del usuario.
