from copy import deepcopy
import numpy as np
from scipy import interp
from sklearn.base import BaseEstimator, TransformerMixin, ClassifierMixin, clone
from sklearn.cross_decomposition.pls_ import PLSRegression, _PLS
from sklearn.model_selection import BaseCrossValidator, KFold
from sklearn.model_selection._split import BaseShuffleSplit
from sklearn.linear_model import LogisticRegression
from sklearn import metrics
from .ChemometricsPLS import ChemometricsPLS
from .ChemometricsScaler import ChemometricsScaler

__author__ = 'gd2212'


class ChemometricsPLS_Logistic(ChemometricsPLS, ClassifierMixin):
    """

    ChemometricsPLS object - Wrapper for sklearn.cross_decomposition PLS algorithms, with tailored methods
    for Chemometric Data analysis.

    :param int ncomps: Number of PLS components desired.
    :param sklearn._PLS pls_algorithm: Scikit-learn PLS algorithm to use - PLSRegression or PLSCanonical are supported.
    :param xscaler: Scaler object for X data matrix.
    :type xscaler: ChemometricsScaler object, scaling/preprocessing objects from scikit-learn or None.
    :param yscaler: Scaler object for the Y data vector/matrix.
    :type yscaler: ChemometricsScaler object, scaling/preprocessing objects from scikit-learn or None.
    :param kwargs pls_type_kwargs: Keyword arguments to be passed during initialization of pls_algorithm.
    :raise TypeError: If the pca_algorithm or scaler objects are not of the right class.
    """

    """
    PLS - DA (y with dummy matrix) followed by Logistic Regression
    The underlying PLS-DA model is exactly the same as standard PLS, and this objects inherits from ChemometricsPLS.
    The PLS scores are then provided for a multivariate logistic regression model. Since PLS components have orthogonal 
    scores, and this is a dimensionality reduction method, there is no need for regularization in the logistic model.
    Interpretation of the model is performed as follows:
    1) Idenfitying the regression coefficients from the Logistic regression models which are relevant 
    2) Anal, without forgetting that the first "predictive" component is related  
    """

    def __init__(self, ncomps=2, pls_algorithm=PLSRegression, logreg_algorithm=LogisticRegression,
                 xscaler=ChemometricsScaler(), yscaler=None, **pls_type_kwargs):

        try:
            # Perform the check with is instance but avoid abstract base class runs.
            pls_algorithm = pls_algorithm(ncomps, scale=False, **pls_type_kwargs)
            if not isinstance(pls_algorithm, (BaseEstimator, _PLS)):
                raise TypeError("Scikit-learn model please")

            if not (isinstance(xscaler, TransformerMixin) or xscaler is None):
                raise TypeError("Scikit-learn Transformer-like object or None")

            logreg_algorithm = logreg_algorithm()
            if not isinstance(logreg_algorithm, (BaseEstimator, LogisticRegression)):
                raise TypeError("Scikit-learn LogisticRegression please")
            # 2 blocks of data = two scaling options
            if xscaler is None:
                xscaler = ChemometricsScaler(0, with_std=False)
                # Force scaling to false, as this will be handled by the provided scaler or not
            # in these PLS + Logistic/LDA the y scaling will not be used anyway, but in the future might be
            if yscaler is None:
                # Remove intercept in this case - So dummy matrix is preserved
                yscaler = ChemometricsScaler(0, with_std=False, with_mean=False)

            self.pls_algorithm = pls_algorithm
            self.logreg_algorithm = logreg_algorithm
            # Most initialized as None, before object is fitted...
            self.scores_t = None
            self.scores_u = None
            self.weights_w = None
            self.weights_c = None
            self.loadings_p = None
            self.loadings_q = None
            self.rotations_ws = None
            self.rotations_cs = None
            self.b_u = None
            self.b_t = None
            self.beta_coeffs = None
            self.logistic_coefs = None

            self._ncomps = ncomps
            self._x_scaler = xscaler
            self._y_scaler = yscaler
            self.cvParameters = None
            self.modelParameters = None
            self._isfitted = False

        except TypeError as terp:
            print(terp.args[0])

    def fit(self, x, y, **fit_params):
        """

        Perform model fitting on the provided x and y data and calculate basic goodness-of-fit metrics.
        Similar to scikit-learn's BaseEstimator method.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features].
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features].
        :param kwargs fit_params: Keyword arguments to be passed to the .fit() method of the core sklearn model.
        :raise ValueError: If any problem occurs during fitting.
        """
        try:
            # This scaling check is always performed to ensure running model with scaling or with scaling == None
            # always gives consistent results (the same type of data scale used fitting will be expected or returned
            # by all methods of the ChemometricsPLS object)
            # For no scaling, mean centering is performed nevertheless - sklearn objects
            # do this by default, this is solely to make everything ultra clear and to expose the
            # interface for potential future modification
            # Comply with the sklearn-scaler behaviour convention
            if y.ndim == 1:
                y = y.reshape(-1, 1)
            #if x.ndim == 1:
            #    x = x.reshape(-1, 1)

            xscaled = self.x_scaler.fit_transform(x)
            yscaled = self.y_scaler.fit_transform(y)

            ## TO DO:
            # Add type checking for dummy matrix and check the scaling is not damaging it. For now should be ok.

            self.pls_algorithm.fit(xscaled, yscaled, **fit_params)

            # Expose the model parameters
            self.loadings_p = self.pls_algorithm.x_loadings_
            self.loadings_q = self.pls_algorithm.y_loadings_
            self.weights_w = self.pls_algorithm.x_weights_
            self.weights_c = self.pls_algorithm.y_weights_
            self.rotations_ws = self.pls_algorithm.x_rotations_
            # scikit learn sets the rotation, causing a discrepancy between the scores calculated during fitting and the transform method
            # for now, we calculate the rotation and override it: C* = pinv(CQ')C
            self.rotations_cs = np.dot(np.linalg.pinv(np.dot(self.weights_c, self.loadings_q.T)), self.weights_c)
            self.scores_t = self.pls_algorithm.x_scores_
            self.scores_u = self.pls_algorithm.y_scores_
            self.b_u = np.dot(np.dot(np.linalg.pinv(np.dot(self.scores_u.T, self.scores_u)), self.scores_u.T),
                              self.scores_t)
            self.b_t = np.dot(np.dot(np.linalg.pinv(np.dot(self.scores_t.T, self.scores_t)), self.scores_t.T),
                              self.scores_u)
            self.beta_coeffs = self.pls_algorithm.coef_
            # Needs to come here for the method shortcuts down the line to work...
            self._isfitted = True

            # Calculate RSSy/RSSx, R2Y/R2X
            # Method inheritance
            R2Y = ChemometricsPLS.score(self, x=x, y=y, block_to_score='y')
            R2X = ChemometricsPLS.score(self, x=x, y=y, block_to_score='x')

            self.logreg_algorithm.fit(self.scores_t, y)

            self.logistic_coefs = self.logreg_algorithm.coef_
            class_score = self.logreg_algorithm.decision_function(self.scores_t)

            y_pred = self.logreg_algorithm.predict(self.scores_t)
            accuracy = metrics.accuracy_score(y, y_pred)
            precision = metrics.precision_score(y, y_pred)
            recall = metrics.recall_score(y, y_pred)
            misclassified_samples = np.where(y.ravel() != y_pred.ravel())[0]
            auc_area = metrics.roc_auc_score(y, class_score)
            f1_score = metrics.f1_score(y, y_pred)
            conf_matrix = metrics.confusion_matrix(y, y_pred)
            class_score = self.logreg_algorithm.decision_function(self.scores_t)
            roc_curve = metrics.roc_curve(y, class_score)
            zero_oneloss = metrics.zero_one_loss(y, y_pred)
            probability = self.logreg_algorithm.predict_proba(self.scores_t)
            log_loss = metrics.log_loss(y, y_pred)
            matthews_mcc = metrics.matthews_corrcoef(y, y_pred)
            # Obtain residual sum of squares for whole data set and per component
            cm_fit = self._cummulativefit(x, y)

            self.modelParameters = {'PLS': {'R2Y': R2Y, 'R2X': R2X, 'SSX': cm_fit['SSX'], 'SSY': cm_fit['SSY'],
                                    'SSXcomp': cm_fit['SSXcomp'], 'SSYcomp': cm_fit['SSYcomp']},
                                    'Logistic': {'Accuracy': accuracy, 'AUC': auc_area,
                                                 'ConfusionMatrix': conf_matrix, 'ROC': roc_curve,
                                                 'MisclassifiedSamples': misclassified_samples,
                                                 'Precision': precision, 'Recall': recall,
                                                 'F1': f1_score, '0-1Loss': zero_oneloss, 'MatthewsMCC': matthews_mcc,
                                                 'Probability': probability, 'LogLoss': log_loss,
                                                 'ClassPredictions': y_pred}}

        except ValueError as verr:
            raise verr

    def fit_transform(self, x, y, **fit_params):
        """

        Fit a model to supplied data and return the scores. Equivalent to scikit-learn's TransformerMixin method.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features].
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features].
        :param kwargs fit_params: Optional keyword arguments to be passed to the pls_algorithm .fit() method.
        :return: Latent Variable scores (T) for the X matrix and for the Y vector/matrix (U).
        :rtype: tuple of numpy.ndarray, shape [[n_tscores], [n_uscores]]
        :raise ValueError: If any problem occurs during fitting.
        """

        try:
            self.fit(x, y, **fit_params)
            # Comply with the sklearn scaler behaviour - y vector doesn't work
            #if y.ndim == 1:
            #    y = y.reshape(-1, 1)
            if x.ndim == 1:
                x = x.reshape(-1, 1)

            xscaled = self.x_scaler.fit_transform(x)
            yscaled = self.y_scaler.fit_transform(y)

            return self.transform(xscaled, y=None), self.transform(x=None, y=yscaled)

        except ValueError as verr:
            raise verr

    def transform(self, x=None, y=None):
        """

        Calculate the scores for a data block from the original data. Equivalent to sklearn's TransformerMixin method.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features] or None
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features] or None
        :return: Latent Variable scores (T) for the X matrix and for the Y vector/matrix (U).
        :rtype: tuple with 2 numpy.ndarray, shape [n_samples, n_comps]
        :raise ValueError: If dimensions of input data are mismatched.
        :raise AttributeError: When calling the method before the model is fitted.
        """

        try:

            # Check if model is fitted
            if self._isfitted is True:
                # If X and Y are passed, complain and do nothing
                if (x is not None) and (y is not None):
                    raise ValueError('xx')
                # If nothing is passed at all, complain and do nothing
                elif (x is None) and (y is None):
                    raise ValueError('yy')
                # If Y is given, return U
                elif x is None:
                    if y.ndim == 1:
                        y = y.reshape(-1, 1)

                    yscaled = self.y_scaler.transform(y)
                    # Taking advantage of rotations_y
                    # Otherwise this would be the full calculation U = Y*pinv(CQ')*C
                    U = np.dot(yscaled, self.rotations_cs)
                    return U

                # If X is given, return T
                elif y is None:
                    # Comply with the sklearn scaler behaviour
                    if x.ndim == 1:
                        x = x.reshape(-1, 1)

                    xscaled = self.x_scaler.transform(x)
                    # Taking advantage of the rotation_x
                    # Otherwise this would be would the full calculation T = X*pinv(WP')*W
                    T = np.dot(xscaled, self.rotations_ws)
                    return T
            else:
                raise AttributeError('Model not fitted')

        except ValueError as verr:
            raise verr
        except AttributeError as atter:
            raise atter

    def inverse_transform(self, t=None, u=None):
        """

        Transform scores to the original data space using their corresponding loadings.
        Same logic as in scikit-learn's TransformerMixin method.

        :param t: T scores corresponding to the X data matrix.
        :type t: numpy.ndarray, shape [n_samples, n_comps] or None
        :param u: Y scores corresponding to the Y data vector/matrix.
        :type u: numpy.ndarray, shape [n_samples, n_comps] or None
        :return x: X Data matrix in the original data space.
        :rtype: numpy.ndarray, shape [n_samples, n_features] or None
        :return y: Y Data matrix in the original data space.
        :rtype: numpy.ndarray, shape [n_samples, n_features] or None
        :raise ValueError: If dimensions of input data are mismatched.
        """
        try:
            if self._isfitted is True:
                if t is not None and u is not None:
                    raise ValueError('xx')
                # If nothing is passed at all, complain and do nothing
                elif t is None and u is None:
                    raise ValueError('yy')
                # If T is given, return U
                elif t is not None:
                    # Calculate X from T using X = TP'
                    xpred = np.dot(t, self.loadings_p.T)
                    if self.x_scaler is not None:
                        xscaled = self.x_scaler.inverse_transform(xpred)
                    else:
                        xscaled = xpred

                    return xscaled
                # If U is given, return T
                elif u is not None:
                    # Calculate Y from U - using Y = UQ'
                    ypred = np.dot(u, self.loadings_q.T)
                    if self.y_scaler is not None:
                        yscaled = self.y_scaler.inverse_transform(ypred)
                    else:
                        yscaled = ypred

                    return yscaled

        except ValueError as verr:
            raise verr

    def score(self, x, y, sample_weight=None):
        """

        Predict and calculate the R2 for the model using one of the data blocks (X or Y) provided.
        Equivalent to the scikit-learn ClassifierMixin score method.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features] or None
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features] or None
        :param str block_to_score: Which of the data blocks (X or Y) to calculate the R2 goodness of fit.
        :param sample_weight: Optional sample weights to use in scoring.
        :type sample_weight: numpy.ndarray, shape [n_samples] or None
        :return R2Y: The model's R2Y, calculated by predicting Y from X and scoring.
        :rtype: float
        :return R2X: The model's R2X, calculated by predicting X from Y and scoring.
        :rtype: float
        :raise ValueError: If block to score argument is not acceptable or date mismatch issues with the provided data.
        """
        # TO DO: actually use sample_weight
        try:
            return metrics.accuracy_score(y, self.predict(x), sample_weight=sample_weight)
        except ValueError as verr:
            raise verr

    def predict(self, x):
        """

        Predict the values in one data block using the other. Same as its scikit-learn's RegressorMixin namesake method.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features] or None
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features] or None
        :return: Predicted data block (X or Y) obtained from the other data block.
        :rtype: numpy.ndarray, shape [n_samples, n_features]
        :raise ValueError: If no data matrix is passed, or dimensions mismatch issues with the provided data.
        :raise AttributeError: Calling the method without fitting the model before.
        """

        try:
            if self._isfitted is False:
                raise AttributeError("Model is not fitted")

            if x.ndim == 1:
                x = x.reshape(-1, 1)
            xscaled = self.x_scaler.transform(x)
            scores = self.transform(x=x)
            return self.logreg_algorithm.predict(scores)

        except ValueError as verr:
            raise verr
        except AttributeError as atter:
            raise atter

    @property
    def ncomps(self):
        try:
            return self._ncomps
        except AttributeError as atre:
            raise atre

    @ncomps.setter
    def ncomps(self, ncomps=1):
        """

        Setter for number of components. Re-sets the model.

        :param int ncomps: Number of PLS components to use in the model.
        :raise AttributeError: If there is a problem changing the number of components and resetting the model.
        """
        # To ensure changing number of components effectively resets the model
        try:
            self._ncomps = ncomps
            self.pls_algorithm = clone(self.pls_algorithm, safe=True)
            self.pls_algorithm.n_components = ncomps
            self.loadings_p = None
            self.scores_t = None
            self.scores_u = None
            self.loadings_q = None
            self.weights_c = None
            self.weights_w = None
            self.rotations_cs = None
            self.rotations_ws = None
            self.cvParameters = None
            self.modelParameters = None
            self.b_t = None
            self.b_u = None
            self.beta_coeffs = None
            self.logreg_algorithm = LogisticRegression
            self.logistic_coefs = None

            return None
        except AttributeError as atre:
            raise atre

    @property
    def x_scaler(self):
        try:
            return self._x_scaler
        except AttributeError as atre:
            raise atre

    @x_scaler.setter
    def x_scaler(self, scaler):
        """

        Setter for the X data block scaler.

        :param scaler: The object which will handle data scaling.
        :type scaler: ChemometricsScaler object, scaling/preprocessing objects from scikit-learn or None
        :raise AttributeError: If there is a problem changing the scaler and resetting the model.
        :raise TypeError: If the new scaler provided is not a valid object.
        """

        try:

            if not (isinstance(scaler, TransformerMixin) or scaler is None):
                raise TypeError("Scikit-learn Transformer-like object or None")
            if scaler is None:
                scaler = ChemometricsScaler(0, with_std=False)

            self._x_scaler = scaler
            self.pls_algorithm = clone(self.pls_algorithm, safe=True)
            self.modelParameters = None
            self.cvParameters = None
            self.loadings_p = None
            self.weights_w = None
            self.weights_c = None
            self.loadings_q = None
            self.rotations_ws = None
            self.rotations_cs = None
            self.scores_t = None
            self.scores_u = None
            self.b_t = None
            self.b_u = None
            self.beta_coeffs = None
            self.logreg_algorithm = LogisticRegression
            self.logistic_coefs = None

            return None
        except AttributeError as atre:
            raise atre
        except TypeError as typerr:
            raise typerr

    @property
    def y_scaler(self):
        try:
            return self._y_scaler
        except AttributeError as atre:
            raise atre

    @y_scaler.setter
    def y_scaler(self, scaler):
        """

        Setter for the Y data block scaler.

        :param scaler: The object which will handle data scaling.
        :type scaler: ChemometricsScaler object, scaling/preprocessing objects from scikit-learn or None
        :raise AttributeError: If there is a problem changing the scaler and resetting the model.
        :raise TypeError: If the new scaler provided is not a valid object.
        """
        try:
            if not (isinstance(scaler, TransformerMixin) or scaler is None):
                raise TypeError("Scikit-learn Transformer-like object or None")
            if scaler is None:
                scaler = ChemometricsScaler(0, with_std=False, with_mean=False)

            self._y_scaler = scaler
            self.pls_algorithm = clone(self.pls_algorithm, safe=True)
            self.modelParameters = None
            self.cvParameters = None
            self.loadings_p = None
            self.weights_w = None
            self.weights_c = None
            self.loadings_q = None
            self.rotations_ws = None
            self.rotations_cs = None
            self.scores_t = None
            self.scores_u = None
            self.b_t = None
            self.b_u = None
            self.beta_coeffs = None
            self.logreg_algorithm = LogisticRegression
            self.logistic_coefs = None

            return None

        except AttributeError as atre:
            raise atre
        except TypeError as typerr:
            raise typerr

    def VIP(self, mode='w', direction='y'):
        """

        Output the Variable importance for projection metric (VIP). With the default values it is calculated
        using the x variable weights and the variance explained of y.

        :param mode: The type of model parameter to use in calculating the VIP. Default value is weights (w), and other acceptable arguments are p, ws, cs, c and q.
        :type mode: str
        :param str direction: The data block to be used to calculated the model fit and regression sum of squares.
        :return numpy.ndarray VIP: The vector with the calculated VIP values.
        :rtype: numpy.ndarray, shape [n_features]
        :raise ValueError: If mode or direction is not a valid option.
        :raise AttributeError: Calling method without a fitted model.
        """

        try:

            if self._isfitted is False:
                raise AttributeError("Model is not fitted")
            if mode not in ['w', 'p', 'ws', 'cs', 'c', 'q']:
                raise ValueError("Invalid type of VIP coefficient")
            if direction not in ['x', 'y']:
                raise ValueError("direction must be x or y")

            choices = {'w': self.weights_w, 'p': self.loadings_p, 'ws': self.rotations_ws, 'cs': self.rotations_cs,
                       'c': self.weights_c, 'q': self.loadings_q}

            if direction == 'y':
                ss_dir = 'SSYcomp'
            else:
                ss_dir = 'SSXcomp'

            nvars = self.loadings_p.shape[0]
            vipnum = np.zeros(nvars)
            for comp in range(0, self.ncomps):
                vipnum += (choices[mode][:, comp] ** 2) * (self.modelParameters['PLS'][ss_dir][comp])

            vip = np.sqrt(vipnum * nvars / self.modelParameters['PLS'][ss_dir].sum())

            return vip

        except AttributeError as atter:
            raise atter
        except ValueError as verr:
            raise verr

    def cross_validation(self, x, y, cv_method=KFold(7, True), outputdist=False, testset_scale=False,
                         **crossval_kwargs):
        """

        Cross-validation method for the model. Calculates Q2 and cross-validated estimates for all model parameters.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features]
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features]
        :param cv_method: An instance of a scikit-learn CrossValidator object.
        :type cv_method: BaseCrossValidator or BaseShuffleSplit
        :param bool outputdist: Output the whole distribution for. Useful when ShuffleSplit or CrossValidators other than KFold.
        :param bool testset_scale: Scale the test sets using its own mean and standard deviation instead of the scaler fitted on training set.
        :param kwargs crossval_kwargs: Keyword arguments to be passed to the sklearn.Pipeline during cross-validation
        :return:
        :rtype: dict
        :raise TypeError: If the cv_method passed is not a scikit-learn CrossValidator object.
        :raise ValueError: If the x and y data matrices are invalid.
        """

        try:
            if not (isinstance(cv_method, BaseCrossValidator) or isinstance(cv_method, BaseShuffleSplit)):
                raise TypeError("Scikit-learn cross-validation object please")

            # Check if global model is fitted... and if not, fit it using all of X
            if self._isfitted is False:
                self.fit(x, y)

            # Make a copy of the object, to ensure the internal state doesn't come out differently from the
            # cross validation method call...
            cv_pipeline = deepcopy(self)
            ncvrounds = cv_method.get_n_splits()

            if x.ndim > 1:
                x_nvars = x.shape[1]
            else:
                x_nvars = 1

            if y.ndim > 1:
                y_nvars = y.shape[1]
            else:
                y_nvars = 1
                y = y.reshape(-1, 1)

            # Initialize list structures to contain the fit
            cv_loadings_p = np.zeros((ncvrounds, x_nvars, self.ncomps))
            cv_loadings_q = np.zeros((ncvrounds, y_nvars, self.ncomps))
            cv_weights_w = np.zeros((ncvrounds, x_nvars, self.ncomps))
            cv_weights_c = np.zeros((ncvrounds, y_nvars, self.ncomps))
            cv_train_scores_t = list()
            cv_train_scores_u = list()

            # CV test scores more informative for ShuffleSplit than KFold but kept here
            cv_test_scores_t = list()
            cv_test_scores_u = list()

            cv_rotations_ws = np.zeros((ncvrounds, x_nvars, self.ncomps))
            cv_rotations_cs = np.zeros((ncvrounds, y_nvars, self.ncomps))
            cv_betacoefs = np.zeros((ncvrounds, x_nvars))
            cv_vipsw = np.zeros((ncvrounds, x_nvars))

            cv_logisticcoefs = np.zeros((ncvrounds, self.ncomps))

            cv_trainprecision = np.zeros(ncvrounds)
            cv_trainrecall = np.zeros(ncvrounds)
            cv_trainaccuracy = np.zeros(ncvrounds)
            cv_trainauc = np.zeros(ncvrounds)
            cv_trainmatthews_mcc = np.zeros(ncvrounds)
            cv_trainzerooneloss = np.zeros(ncvrounds)
            cv_trainf1 = np.zeros(ncvrounds)
            cv_trainprobability = list()
            cv_trainclasspredictions = list()
            cv_trainroc_curve = list()
            cv_trainconfusionmatrix = list()
            cv_trainmisclassifiedsamples = list()

            cv_testprecision = np.zeros(ncvrounds)
            cv_testrecall = np.zeros(ncvrounds)
            cv_testaccuracy = np.zeros(ncvrounds)
            cv_testauc = np.zeros(ncvrounds)
            cv_testmatthews_mcc = np.zeros(ncvrounds)
            cv_testzerooneloss = np.zeros(ncvrounds)
            cv_testf1 = np.zeros(ncvrounds)
            cv_testprobability = list()
            cv_testclasspredictions = list()
            cv_testroc_curve = list()
            cv_testconfusionmatrix = list()
            cv_testmisclassifiedsamples = list()

            # Initialise predictive residual sum of squares variable (for whole CV routine)
            pressy = 0
            pressx = 0

            # Calculate Sum of Squares SS in whole dataset for future calculations
            ssx = np.sum((cv_pipeline.x_scaler.fit_transform(x)) ** 2)
            ssy = np.sum((cv_pipeline.y_scaler.fit_transform(y)) ** 2)

            # As assessed in the test set..., opposed to PRESS
            R2X_training = np.zeros(ncvrounds)
            R2Y_training = np.zeros(ncvrounds)
            # R2X and R2Y assessed in the test set
            R2X_test = np.zeros(ncvrounds)
            R2Y_test = np.zeros(ncvrounds)

            for cvround, train_testidx in enumerate(cv_method.split(x, y)):
                # split the data explicitly
                train = train_testidx[0]
                test = train_testidx[1]

                # Check dimensions for the indexing
                if y_nvars == 1:
                    ytrain = y[train]
                    ytest = y[test]
                else:
                    ytrain = y[train, :]
                    ytest = y[test, :]
                if x_nvars == 1:
                    xtrain = x[train]
                    xtest = x[test]
                else:
                    xtrain = x[train, :]
                    xtest = x[test, :]

                cv_pipeline.fit(xtrain, ytrain, **crossval_kwargs)
                # Prepare the scaled X and Y test data
                # If testset_scale is True, these are scaled individually...

                # Comply with the sklearn scaler behaviour
                if ytest.ndim == 1:
                    ytest = ytest.reshape(-1, 1)
                    ytrain = ytrain.reshape(-1, 1)
                if xtest.ndim == 1:
                    xtest = xtest.reshape(-1, 1)
                    xtrain = xtrain.reshape(-1, 1)
                # Fit the training data

                if testset_scale is True:
                    xtest_scaled = cv_pipeline.x_scaler.fit_transform(xtest)
                    ytest_scaled = cv_pipeline.y_scaler.fit_transform(ytest)
                # Otherwise (default), training set mean and scaling vectors are used
                else:
                    xtest_scaled = cv_pipeline.x_scaler.transform(xtest)
                    ytest_scaled = cv_pipeline.y_scaler.transform(ytest)

                R2X_training[cvround] = ChemometricsPLS.score(cv_pipeline, xtrain, ytrain, 'x')
                R2Y_training[cvround] = ChemometricsPLS.score(cv_pipeline, xtrain, ytrain, 'y')
                # Use super here  for Q2
                ypred = ChemometricsPLS.predict(cv_pipeline, x=xtest, y=None)
                xpred = ChemometricsPLS.predict(cv_pipeline, x=None, y=ytest)

                xpred = cv_pipeline.x_scaler.transform(xpred).squeeze()
                ypred = cv_pipeline.y_scaler.transform(ypred).squeeze()
                ytest_scaled = ytest_scaled.squeeze()

                curr_pressx = np.sum((xtest_scaled - xpred) ** 2)
                curr_pressy = np.sum((ytest_scaled - ypred) ** 2)

                R2X_test[cvround] = ChemometricsPLS.score(cv_pipeline, xtest, ytest, 'x')
                R2Y_test[cvround] = ChemometricsPLS.score(cv_pipeline, xtest, ytest, 'y')

                pressx += curr_pressx
                pressy += curr_pressy

                cv_loadings_p[cvround, :, :] = cv_pipeline.loadings_p
                cv_loadings_q[cvround, :, :] = cv_pipeline.loadings_q
                cv_weights_w[cvround, :, :] = cv_pipeline.weights_w
                cv_weights_c[cvround, :, :] = cv_pipeline.weights_c
                cv_rotations_ws[cvround, :, :] = cv_pipeline.rotations_ws
                cv_rotations_cs[cvround, :, :] = cv_pipeline.rotations_cs
                cv_betacoefs[cvround, :] = cv_pipeline.beta_coeffs.T
                cv_vipsw[cvround, :] = cv_pipeline.VIP()
                cv_logisticcoefs[cvround] = cv_pipeline.logistic_coefs

                # Training metrics
                cv_trainaccuracy[cvround] = cv_pipeline.modelParameters['Logistic']['Accuracy']
                cv_trainprecision[cvround] = cv_pipeline.modelParameters['Logistic']['Precision']
                cv_trainrecall[cvround] = cv_pipeline.modelParameters['Logistic']['Recall']
                cv_trainauc[cvround] = cv_pipeline.modelParameters['Logistic']['AUC']
                cv_trainf1[cvround] = cv_pipeline.modelParameters['Logistic']['F1']
                cv_trainmatthews_mcc[cvround] = cv_pipeline.modelParameters['Logistic']['MatthewsMCC']
                cv_trainzerooneloss[cvround] = cv_pipeline.modelParameters['Logistic']['0-1Loss']

                # Check this indexes, same as CV scores
                cv_trainmisclassifiedsamples.append(train[cv_pipeline.modelParameters['Logistic']['MisclassifiedSamples']])
                cv_trainmisclassifiedsamples.append([*zip(train, cv_pipeline.modelParameters['Logistic']['ClassPredictions'])])
                cv_trainprobability.append([*zip(train, cv_pipeline.modelParameters['Logistic']['Probability'])])

                # TODO: add the roc curve interpolation in the end
                cv_trainroc_curve.append(cv_pipeline.modelParameters['Logistic']['ROC'])

                testscores = cv_pipeline.transform(x=xtest)
                class_score = cv_pipeline.logreg_algorithm.decision_function(testscores)

                y_pred = cv_pipeline.predict(xtest)
                test_accuracy = metrics.accuracy_score(ytest, y_pred)
                test_precision = metrics.precision_score(ytest, y_pred)
                test_recall = metrics.recall_score(ytest, y_pred)
                test_auc_area = metrics.roc_auc_score(ytest, class_score)
                test_f1_score = metrics.f1_score(ytest, y_pred)
                test_zero_oneloss = metrics.zero_one_loss(ytest, y_pred)
                test_matthews_mcc = metrics.matthews_corrcoef(ytest, y_pred)
                # Obtain residual sum of squares for whole data set and per component

                # Check the actual indexes in the original samples
                test_misclassified_samples = test[np.where(ytest.ravel() != y_pred.ravel())[0]]
                test_probability = cv_pipeline.logreg_algorithm.predict_proba(testscores)
                test_classpredictions = [*zip(test, y_pred)]
                test_conf_matrix = metrics.confusion_matrix(ytest, y_pred)

                # TODO: Add ROC curve interpolation in the end
                test_roc_curve = metrics.roc_curve(ytest, class_score)

                # Test metrics
                cv_testaccuracy[cvround] = test_accuracy
                cv_testprecision[cvround] = test_precision
                cv_testrecall[cvround] = test_recall
                cv_testauc[cvround] = test_auc_area
                cv_testf1[cvround] = test_f1_score
                cv_testmatthews_mcc[cvround] = test_matthews_mcc
                cv_testzerooneloss[cvround] = test_zero_oneloss
                # Check this indexes, same as CV scores
                cv_testmisclassifiedsamples.append(test_misclassified_samples)
                cv_testroc_curve.append(test_roc_curve)
                cv_testprobability.append(test_probability)
                cv_testconfusionmatrix.append(test_conf_matrix)
                cv_testclasspredictions.append(test_classpredictions)

            # Align model parameters to account for sign indeterminacy.
            # The criteria here used is to select the sign that gives a more similar profile (by L1 distance) to the loadings from
            # on the model fitted with the whole data. Any other parameter can be used, but since the loadings in X capture
            # the covariance structure in the X data block, in theory they should have more pronounced features even in cases of
            # null X-Y association, making the sign flip more resilient.
            for cvround in range(0, ncvrounds):
                for currload in range(0, self.ncomps):
                    # evaluate based on loadings _p
                    choice = np.argmin(
                        np.array([np.sum(np.abs(self.loadings_p[:, currload] - cv_loadings_p[cvround, :, currload])),
                                  np.sum(np.abs(
                                      self.loadings_p[:, currload] - cv_loadings_p[cvround, :, currload] * -1))]))
                    if choice == 1:
                        cv_loadings_p[cvround, :, currload] = -1 * cv_loadings_p[cvround, :, currload]
                        cv_loadings_q[cvround, :, currload] = -1 * cv_loadings_q[cvround, :, currload]
                        cv_weights_w[cvround, :, currload] = -1 * cv_weights_w[cvround, :, currload]
                        cv_weights_c[cvround, :, currload] = -1 * cv_weights_c[cvround, :, currload]
                        cv_rotations_ws[cvround, :, currload] = -1 * cv_rotations_ws[cvround, :, currload]
                        cv_rotations_cs[cvround, :, currload] = -1 * cv_rotations_cs[cvround, :, currload]
                        cv_train_scores_t.append([*zip(train, -1*cv_pipeline.scores_t)])
                        cv_train_scores_u.append([*zip(train, -1*cv_pipeline.scores_u)])
                        cv_test_scores_t.append([*zip(test, -1*cv_pipeline.scores_t)])
                        cv_test_scores_u.append([*zip(test, -1*cv_pipeline.scores_u)])
                    else:
                        cv_train_scores_t.append([*zip(train, cv_pipeline.scores_t)])
                        cv_train_scores_u.append([*zip(train, cv_pipeline.scores_u)])
                        cv_test_scores_t.append([*zip(test, cv_pipeline.scores_t)])
                        cv_test_scores_u.append([*zip(test, cv_pipeline.scores_u)])



            # Calculate total sum of squares
            q_squaredy = 1 - (pressy / ssy)
            q_squaredx = 1 - (pressx / ssx)

            # Store everything...
            self.cvParameters = {'PLS': {'Q2X': q_squaredx, 'Q2Y': q_squaredy,
                                 'MeanR2X_Training': np.mean(R2X_training),
                                 'MeanR2Y_Training': np.mean(R2Y_training),
                                 'StdevR2X_Training': np.std(R2X_training),
                                 'StdevR2Y_Training': np.std(R2X_training),
                                 'MeanR2X_Test': np.mean(R2X_test),
                                 'MeanR2Y_Test': np.mean(R2Y_test),
                                 'StdevR2X_Test': np.std(R2X_test),
                                 'StdevR2Y_Test': np.std(R2Y_test)},
                                 'Logistic': dict()}

            # Means and standard deviations...
            self.cvParameters['PLS']['Mean_Loadings_q'] = cv_loadings_q.mean(0)
            self.cvParameters['PLS']['Stdev_Loadings_q'] = cv_loadings_q.std(0)
            self.cvParameters['PLS']['Mean_Loadings_p'] = cv_loadings_p.mean(0)
            self.cvParameters['PLS']['Stdev_Loadings_p'] = cv_loadings_q.std(0)
            self.cvParameters['PLS']['Mean_Weights_c'] = cv_weights_c.mean(0)
            self.cvParameters['PLS']['Stdev_Weights_c'] = cv_weights_c.std(0)
            self.cvParameters['PLS']['Mean_Weights_w'] = cv_weights_w.mean(0)
            self.cvParameters['PLS']['Stdev_Loadings_w'] = cv_weights_w.std(0)
            self.cvParameters['PLS']['Mean_Rotations_ws'] = cv_rotations_ws.mean(0)
            self.cvParameters['PLS']['Stdev_Rotations_ws'] = cv_rotations_ws.std(0)
            self.cvParameters['PLS']['Mean_Rotations_cs'] = cv_rotations_cs.mean(0)
            self.cvParameters['PLS']['Stdev_Rotations_cs'] = cv_rotations_cs.std(0)
            self.cvParameters['PLS']['Mean_Beta'] = cv_betacoefs.mean(0)
            self.cvParameters['PLS']['Stdev_Beta'] = cv_betacoefs.std(0)
            self.cvParameters['PLS']['Mean_VIP'] = cv_vipsw.mean(0)
            self.cvParameters['PLS']['Stdev_VIP'] = cv_vipsw.std(0)
            self.cvParameters['Logistic']['Mean_MCC'] = cv_testmatthews_mcc.mean(0)
            self.cvParameters['Logistic']['Stdev_MCC'] = cv_testmatthews_mcc.std(0)
            self.cvParameters['Logistic']['Mean_Recall'] = cv_testrecall.mean(0)
            self.cvParameters['Logistic']['Stdev_Recall'] = cv_testrecall.std(0)
            self.cvParameters['Logistic']['Mean_Precision'] = cv_testprecision.mean(0)
            self.cvParameters['Logistic']['Stdev_Precision'] = cv_testprecision.std(0)
            self.cvParameters['Logistic']['Mean_Accuracy'] = cv_testaccuracy.mean(0)
            self.cvParameters['Logistic']['Stdev_Accuracy'] = cv_testaccuracy.std(0)
            self.cvParameters['Logistic']['Mean_f1'] = cv_testf1.mean(0)
            self.cvParameters['Logistic']['Stdev_f1'] = cv_testf1.std(0)
            self.cvParameters['Logistic']['Mean_0-1Loss'] = cv_testzerooneloss.mean(0)
            self.cvParameters['Logistic']['Stdev_0-1Loss'] = cv_testzerooneloss.std(0)
            self.cvParameters['Logistic']['Mean_Coefs'] = cv_logisticcoefs.mean(0)
            self.cvParameters['Logistic']['Stdev_Coefs'] = cv_logisticcoefs.std(0)

            # Save everything found during CV
            if outputdist is True:
                # Apart from R'2s and scores, the PLS parameters and Logistic regression coefficients are
                #
                self.cvParameters['PLS']['CVR2X_Training'] = R2X_training
                self.cvParameters['PLS']['CVR2Y_Training'] = R2Y_training
                self.cvParameters['PLS']['CVR2X_Test'] = R2X_test
                self.cvParameters['PLS']['CVR2Y_Test'] = R2Y_test
                self.cvParameters['PLS']['CV_Loadings_q'] = cv_loadings_q
                self.cvParameters['PLS']['CV_Loadings_p'] = cv_loadings_p
                self.cvParameters['PLS']['CV_Weights_c'] = cv_weights_c
                self.cvParameters['PLS']['CV_Weights_w'] = cv_weights_w
                self.cvParameters['PLS']['CV_Rotations_ws'] = cv_rotations_ws
                self.cvParameters['PLS']['CV_Rotations_cs'] = cv_rotations_cs
                self.cvParameters['PLS']['CV_TestScores_t'] = cv_test_scores_t
                self.cvParameters['PLS']['CV_TestScores_u'] = cv_test_scores_u
                self.cvParameters['PLS']['CV_TrainScores_t'] = cv_train_scores_t
                self.cvParameters['PLS']['CV_TrainScores_u'] = cv_train_scores_u
                self.cvParameters['PLS']['CV_Beta'] = cv_betacoefs
                self.cvParameters['PLS']['CV_VIPw'] = cv_vipsw

                self.cvParameters['Logistic']['CV_Coefs'] = cv_logisticcoefs
                # CV Test set metrics - The metrics which matter to benchmark classifier
                self.cvParameters['Logistic']['CV_TestMCC'] = cv_testmatthews_mcc
                self.cvParameters['Logistic']['CV_TestRecall'] = cv_testrecall
                self.cvParameters['Logistic']['CV_TestPrecision'] = cv_testprecision
                self.cvParameters['Logistic']['CV_TestAccuracy'] = cv_testaccuracy
                self.cvParameters['Logistic']['CV_Testf1'] = cv_testf1
                self.cvParameters['Logistic']['CV_Test0-1Loss'] = cv_testzerooneloss
                self.cvParameters['Logistic']['CV_TestROC'] = cv_testroc_curve
                self.cvParameters['Logistic']['CV_TestConfusionMatrix'] = cv_testconfusionmatrix
                self.cvParameters['Logistic']['CV_TestSamplePrediction'] = cv_testclasspredictions
                self.cvParameters['Logistic']['CV_TestMisclassifiedsamples'] = cv_testmisclassifiedsamples
                # CV Train parameters - so we can keep a look on model performance in training set
                self.cvParameters['Logistic']['CV_TrainMCC'] = cv_trainmatthews_mcc
                self.cvParameters['Logistic']['CV_TrainRecall'] = cv_trainrecall
                self.cvParameters['Logistic']['CV_TrainPrecision'] = cv_trainprecision
                self.cvParameters['Logistic']['CV_TrainAccuracy'] = cv_trainaccuracy
                self.cvParameters['Logistic']['CV_Trainf1'] = cv_trainf1
                self.cvParameters['Logistic']['CV_Train0-1Loss'] = cv_trainzerooneloss
                self.cvParameters['Logistic']['CV_TrainROC'] = cv_trainroc_curve
                self.cvParameters['Logistic']['CV_TrainConfusionMatrix'] = cv_trainconfusionmatrix
                self.cvParameters['Logistic']['CV_TrainSamplePrediction'] = cv_trainclasspredictions
                self.cvParameters['Logistic']['CV_TrainMisclassifiedsamples'] = cv_trainmisclassifiedsamples

            return None

        except TypeError as terp:
            raise terp

    def permutation_test(self, x, y, nperms=1000, cv_method=KFold(7, True), **permtest_kwargs):
        """

        Permutation test for the classifier. Outputs permuted null distributions for model performance metrics (Q2X/Q2Y)
        and most model parameters.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features]
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features]
        :param int nperms: Number of permutations to perform.
        :param cv_method: An instance of a scikit-learn CrossValidator object.
        :type cv_method: BaseCrossValidator or BaseShuffleSplit
        :param kwargs permtest_kwargs: Keyword arguments to be passed to the .fit() method during cross-validation and model fitting.
        :return: Permuted null distributions for model parameters and the permutation p-value for the Q2Y value.
        :rtype: dict
        """
        try:
            # Check if global model is fitted... and if not, fit it using all of X
            if self._isfitted is False or self.loadings_p is None:
                self.fit(x, y, **permtest_kwargs)
            # Make a copy of the object, to ensure the internal state doesn't come out differently from the
            # cross validation method call...
            permute_class = deepcopy(self)

            if x.ndim > 1:
                x_nvars = x.shape[1]
            else:
                x_nvars = 1

            if y.ndim > 1:
                y_nvars = y.shape[1]
            else:
                y_nvars = 1

            # Initialize data structures for permuted distributions
            perm_loadings_q = np.zeros((nperms, y_nvars, self.ncomps))
            perm_loadings_p = np.zeros((nperms, x_nvars, self.ncomps))
            perm_weights_c = np.zeros((nperms, y_nvars, self.ncomps))
            perm_weights_w = np.zeros((nperms, x_nvars, self.ncomps))
            perm_rotations_cs = np.zeros((nperms, y_nvars, self.ncomps))
            perm_rotations_ws = np.zeros((nperms, x_nvars, self.ncomps))
            perm_beta = np.zeros((nperms, x_nvars, y_nvars))
            perm_vipsw = np.zeros((nperms, x_nvars))

            permuted_R2Y = np.zeros(nperms)
            permuted_R2X = np.zeros(nperms)
            permuted_Q2Y = np.zeros(nperms)
            permuted_Q2X = np.zeros(nperms)
            permuted_R2Y_test = np.zeros(nperms)
            permuted_R2X_test = np.zeros(nperms)

            for permutation in range(0, nperms):
                # Copy original column order, shuffle array in place...
                original_Y = np.copy(y)
                np.random.shuffle(y)
                # ... Fit model and replace original data
                permute_class.fit(x, y, **permtest_kwargs)
                permute_class.cross_validation(x, y, cv_method=cv_method, **permtest_kwargs)
                y = original_Y
                permuted_R2Y[permutation] = permute_class.modelParameters['R2Y']
                permuted_R2X[permutation] = permute_class.modelParameters['R2X']
                permuted_Q2Y[permutation] = permute_class.cvParameters['Q2Y']
                permuted_Q2X[permutation] = permute_class.cvParameters['Q2X']

                # Store the loadings for each permutation component-wise
                perm_loadings_q[permutation, :, :] = permute_class.loadings_q
                perm_loadings_p[permutation, :, :] = permute_class.loadings_p
                perm_weights_c[permutation, :, :] = permute_class.weights_c
                perm_weights_w[permutation, :, :] = permute_class.weights_w
                perm_rotations_cs[permutation, :, :] = permute_class.rotations_cs
                perm_rotations_ws[permutation, :, :] = permute_class.rotations_ws
                perm_beta[permutation, :, :] = permute_class.beta_coeffs
                perm_vipsw[permutation, :] = permute_class.VIP()
            # Align model parameters due to sign indeterminacy.
            # Solution provided is to select the sign that gives a more similar profile to the
            # Loadings calculated with the whole data.
            for perm_round in range(0, nperms):
                for currload in range(0, self.ncomps):
                    # evaluate based on loadings _p
                    choice = np.argmin(np.array(
                        [np.sum(np.abs(self.loadings_p[:, currload] - perm_loadings_p[perm_round, :, currload])),
                         np.sum(np.abs(self.loadings_p[:, currload] - perm_loadings_p[perm_round, :, currload] * -1))]))
                    if choice == 1:
                        perm_loadings_p[perm_round, :, currload] = -1 * perm_loadings_p[perm_round, :, currload]
                        perm_loadings_q[perm_round, :, currload] = -1 * perm_loadings_q[perm_round, :, currload]
                        perm_weights_w[perm_round, :, currload] = -1 * perm_weights_w[perm_round, :, currload]
                        perm_weights_c[perm_round, :, currload] = -1 * perm_weights_c[perm_round, :, currload]
                        perm_rotations_ws[perm_round, :, currload] = -1 * perm_rotations_ws[perm_round, :, currload]
                        perm_rotations_cs[perm_round, :, currload] = -1 * perm_rotations_cs[perm_round, :, currload]

            # Pack everything into a nice data structure and return
            # Calculate p-value for Q2Y as well
            permutationTest = dict()
            permutationTest['R2Y'] = permuted_R2Y
            permutationTest['R2X'] = permuted_R2X
            permutationTest['Q2Y'] = permuted_Q2Y
            permutationTest['Q2X'] = permuted_Q2X
            permutationTest['R2Y_Test'] = permuted_R2Y_test
            permutationTest['R2X_Test'] = permuted_R2X_test
            permutationTest['Loadings_p'] = perm_loadings_p
            permutationTest['Loadings_q'] = perm_loadings_q
            permutationTest['Weights_c'] = perm_weights_c
            permutationTest['Weights_w'] = perm_weights_w
            permutationTest['Rotations_ws'] = perm_rotations_ws
            permutationTest['Rotations_cs'] = perm_rotations_cs
            permutationTest['Beta'] = perm_beta
            permutationTest['VIPw'] = perm_vipsw

            obs_q2y = self.cvParameters['Q2Y']
            pvals = dict()
            pvals['Q2Y'] = (len(np.where(permuted_Q2Y >= obs_q2y)) + 1) / (nperms + 1)

            return permutationTest, pvals

        except ValueError as exp:
            raise exp

    def _cummulativefit(self, x, y):
        """
        Measure the cumulative Regression sum of Squares for each individual component.

        :param x: Data matrix to fit the PLS model.
        :type x: numpy.ndarray, shape [n_samples, n_features]
        :param y: Data matrix to fit the PLS model.
        :type y: numpy.ndarray, shape [n_samples, n_features]
        :return: dictionary object containing the total Regression Sum of Squares and the Sum of Squares
        per components, for both the X and Y data blocks.
        :rtype: dict
        """
        if y.ndim == 1:
            y = y.reshape(-1, 1)
        if x.ndim == 1:
            x = x.reshape(-1, 1)

        xscaled = self.x_scaler.fit_transform(x)
        yscaled = self.y_scaler.fit_transform(y)

        ssx_comp = list()
        ssy_comp = list()

        # Obtain residual sum of squares for whole data set and per component
        SSX = np.sum(xscaled ** 2)
        SSY = np.sum(yscaled ** 2)
        ssx_comp = list()
        ssy_comp = list()

        for curr_comp in range(1, self.ncomps + 1):
            model = self._reduce_ncomps(curr_comp)

            ypred = self.y_scaler.transform(ChemometricsPLS.predict(model, x, y=None))
            xpred = self.x_scaler.transform(ChemometricsPLS.predict(model, x=None, y=y))

            rssy = np.sum((yscaled - ypred) ** 2)
            rssx = np.sum((xscaled - xpred) ** 2)
            ssx_comp.append(rssx)
            ssy_comp.append(rssy)

        cumulative_fit = {'SSX': SSX, 'SSY': SSY, 'SSXcomp': np.array(ssx_comp), 'SSYcomp': np.array(ssy_comp)}

        return cumulative_fit

    def _reduce_ncomps(self, ncomps):
        """

        Generate a new model with a smaller set of components.

        :param int ncomps: Number of ordered first N components from the original model to be kept.
        Must be smaller than the ncomps value of the original model.
        :return ChemometricsPLS object with reduced number of components.
        :rtype: ChemometricsPLS
        :raise ValueError: If number of components desired is larger than original number of components
        :raise AttributeError: If model is not fitted.
        """
        try:
            if ncomps > self.ncomps:
                raise ValueError('Fit a new model with more components instead')
            if self._isfitted is False:
                raise AttributeError('Model not Fitted')

            newmodel = deepcopy(self)
            newmodel._ncomps = ncomps

            newmodel.modelParameters = None
            newmodel.cvParameters = None
            newmodel.loadings_p = self.loadings_p[:, 0:ncomps]
            newmodel.weights_w = self.weights_w[:, 0:ncomps]
            newmodel.weights_c = self.weights_c[:, 0:ncomps]
            newmodel.loadings_q = self.loadings_q[:, 0:ncomps]
            newmodel.rotations_ws = self.rotations_ws[:, 0:ncomps]
            newmodel.rotations_cs = self.rotations_cs[:, 0:ncomps]
            newmodel.scores_t = None
            newmodel.scores_u = None
            newmodel.b_t = self.b_t[0:ncomps, 0:ncomps]
            newmodel.b_u = self.b_u[0:ncomps, 0:ncomps]

            # These have to be recalculated from the rotations
            newmodel.beta_coeffs = np.dot(newmodel.rotations_ws, newmodel.loadings_q.T)

            newmodel.logreg_algorithm = None
            # Line also in the original sklearn method, but unnecessary when scaling = False - kept here for testing...
            # newmodel.beta_coeffs = (1. / newmodel.x_scaler.scale_.reshape((newmodel.x_scaler.scale_.shape[0], 1)) *
            #                        newmodel.beta_coeffs * newmodel.y_scaler.scale_)

            # What to do with the reffiting of the classification model?? - Easy way out - Mention
            # That the predictor will be broken, since this is only to use during the crossvalidation anyway...
            #newmodel.logreg_algorithm = log - What about this?
            return newmodel
        except ValueError as verr:
            raise verr
        except AttributeError as atter:
            raise atter

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            setattr(result, k, deepcopy(v, memo))
        return result
