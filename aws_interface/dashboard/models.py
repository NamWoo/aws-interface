import time
from threading import Thread
from contextlib import contextmanager
import uuid
import traceback
from django.db import models, transaction
from dashboard.security.crypto import AESCipher
from django.contrib.auth.models import AbstractUser, BaseUserManager
import cloud.shortuuid as shortuuid
import core.api


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        from dashboard.security.crypto import Salt
        if not email:
            raise ValueError('The given email must be set')

        access_key = extra_fields.pop('aws_access_key', None)
        secret_key = extra_fields.pop('aws_secret_key', None)

        email = self.normalize_email(email)
        user = self.model(username=email, email=email, **extra_fields)
        user.salt = Salt.get_salt(32)
        user.set_password(password)

        if access_key is not None and secret_key is not None:
            user.set_aws_credentials(password, access_key, secret_key);
        else:
            assert(access_key is None and secret_key is None)

        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        """Create and save a regular User with the given email and password."""
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        """Create and save a SuperUser with the given email and password."""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    creation_date = models.DateTimeField(auto_now_add=True, editable=False, null=False, blank=False)

    email = models.CharField(max_length=200, blank=False, unique=True)
    c_aws_access_key = models.CharField(max_length=256, null=True)
    c_aws_secret_key = models.CharField(max_length=256, null=True)
    salt = models.CharField(max_length=512)

    REQUIRED_FIELDS = []
    USERNAME_FIELD = 'email'

    objects = UserManager()

    # the following methods use raw_password, hence should only be
    # at login or registration

    def encrypt(self, raw_password, string):
        # uses raw_password, hence can only be done at login or registration
        from dashboard.security.crypto import AESCipher
        aes = AESCipher(raw_password + self.salt)
        assert (self.check_password(raw_password))
        return aes.encrypt(string)

    def decrypt(self, raw_password, string):
        aes = AESCipher(raw_password + self.salt)
        assert (self.check_password(raw_password))
        return aes.decrypt(string)

    def set_aws_credentials(self, raw_password, access_key, secret_key):
        assert (self.check_password(raw_password))
        self.c_aws_access_key = self.encrypt(raw_password, access_key)
        self.c_aws_secret_key = self.encrypt(raw_password, secret_key)

    def get_aws_access_key(self, raw_password):
        assert (self.check_password(raw_password))
        return self.decrypt(raw_password, self.c_aws_access_key)

    def get_aws_secret_key(self, raw_password):
        assert (self.check_password(raw_password))
        return self.decrypt(raw_password, self.c_aws_secret_key)


class App(models.Model):
    id = models.CharField(max_length=255, primary_key=True, default=shortuuid.uuid, editable=False)
    creation_date = models.DateTimeField(auto_now_add=True, editable=False, null=False, blank=False)
    name = models.CharField(max_length=255, blank=False, unique=True)
    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE)  # should not be NULL from now on

    applying_in_background = models.BooleanField(default=False)

    class Meta:
        unique_together = ('name', 'user')

    def assign_all_recipes(self):
        for recipe in core.api.recipe_list:
            self.recipe_set.create(name=recipe)

    def all_applied(self):
        """
        Check if all recipes are applied
        :return:
        """
        recipes = self.recipe_set.filter(apply_status__in=(Recipe.APPLY_NONE, Recipe.APPLY_FAILED))
        return recipes.count() == 0

    def generate_sdk(self, credentials, platform):
        """
        :return:
        None if recipes are not applied yet
        """
        if not self.all_applied():
            return None
        else:
            recipes = self.recipe_set.all()
            apis = []
            for recipe in recipes:
                apis.append(recipe.get_api(credentials))
            return core.api.generate_sdk(apis, platform)

    def apply_recipes(self, credentials):
        """
        Start a thread to apply all recipes.
        :return:
        If all recipes were already apply, return True.
        """
        if self.all_applied():
            return True

        run = False
        with transaction.atomic():
            app = App.objects.get(id=self.id)
            if not app.applying_in_background:
                App.objects.filter(id=self.id).update(applying_in_background=True)
                run = True
        if run:
            def _apply_api():
                for _ in range(5):
                    recipes = self.recipe_set.filter(apply_status__in=(Recipe.APPLY_NONE, Recipe.APPLY_FAILED))
                    for recipe in recipes:
                        try:
                            print('APPLY API START: {}'.format(str(recipe)))
                            recipe.apply(credentials)
                            print('APPLY API END:   {}'.format(str(recipe)))
                            # in case Recipe in DB has changed
                            self.recipe_set.filter(id=recipe.id).update(apply_status=Recipe.APPLY_SUCCESS)
                        except Exception as e:
                            print('APPLY API ERROR: {}'.format(str(recipe)))
                            # in case Recipe in DB has changed
                            self.recipe_set.filter(id=recipe.id).update(apply_status=Recipe.APPLY_FAILED)
                            print(traceback.format_exc())
                            print(e)
                    # in case App in DB has changed
                    recipes = self.recipe_set.filter(apply_status__in=(Recipe.APPLY_NONE, Recipe.APPLY_FAILED))
                    if recipes.count() != 0:
                        print("{} APPLY'S LEFT: RETRYING SOON...".format(recipes.count()))
                        time.sleep(35)
                    else:
                        print("APPLY COMPLETE")
                        break
                App.objects.all().filter(id=self.id).update(applying_in_background=False)
            Thread(target=_apply_api, args=()).start()
        return False

    def __str__(self):
        return self.name


class Recipe(models.Model):
    APPLY_FAILED = 'FA'
    APPLY_NONE = 'NO'
    APPLY_PROGRESS = 'PR'
    APPLY_WAITING = 'WA'
    APPLY_SUCCESS = 'SU'

    APPLY_STATUS_CHOICES = (
        (APPLY_NONE, 'Not applied'),
        (APPLY_FAILED, 'Apply failed'),
        (APPLY_PROGRESS, 'Applying'),
        (APPLY_SUCCESS, 'Applied'),
    )

    id = models.CharField(max_length=255, primary_key=True, default=shortuuid.uuid, editable=False)
    creation_date = models.DateTimeField(auto_now_add=True, editable=False, null=False, blank=False)
    name = models.CharField(max_length=255, editable=False)
    json_string = models.TextField(default='')
    app = models.ForeignKey(App, null=True, on_delete=models.CASCADE)  # should not be NULL from now on
    apply_status = models.CharField(max_length=2, choices=APPLY_STATUS_CHOICES, default=APPLY_NONE)

    def __str__(self):
        tag = self.name.title() + ' Recipe'
        owner = '{}:{}'.format(self.app.user.email, self.app.name)
        init = self.get_apply_status_display()
        return '{:20} [{:20}] [{:20}]'.format(tag, owner, init)

    def get_api(self, credentials):
        api_cls = core.api.api_dict[self.name]
        return api_cls(credentials, self.app.id, self.json_string)

    def save_recipe(self, api: core.api.API):
        self.json_string = api.get_recipe_controller().to_json()
        self.apply_status = 'NO'
        self.save()

    @contextmanager
    def api(self, credentials):
        """
        You can do this:
            with recipe.api() as api:
                use(api)
        Instead of this:
            api = recipe.api()
            use(api)
            recipe.save_recipe(api)
        :param credentials:
        :return:
        """
        api = self.get_api(credentials)
        yield api
        self.save_recipe(api)

    def apply(self, credentials):
        self.get_api(credentials).apply()
